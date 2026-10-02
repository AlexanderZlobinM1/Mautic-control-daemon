import json
import fcntl
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mcd_agent.native_comparator import (
    ComparatorBusy,
    ComparatorError,
    NativeComparator,
    evidence_sha256,
    missing_cycle_receipt,
    shared_root_fence_path,
    validate_receipt,
)
from mcd_agent.executor import execute_native_comparator_cycle

_TASK_LOCK_BUSY_RC = 75


class NativeComparatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "comparator.sqlite")
        self.comparator = NativeComparator(self.db, str(Path(self.tmp.name) / "locks"))

    def tearDown(self):
        self.tmp.cleanup()

    def pre(self, object_id="42", *, decision="not_started", due=True, complete=True, generation="g1", fingerprint=None, due_since=10):
        fingerprint = fingerprint or "a" * 64
        return {
            "object_id": object_id, "eligible": True, "due": due,
            "input_generation": generation, "watermark": "w1",
            "input_fingerprint": fingerprint, "due_since": due_since,
            "obligation_deadline": 15, "last_eligible_observed_at": 10,
            "history_complete_until": 20,
            "observed_at": 20, "mcd_decision": decision,
            "mcd_start_generation": None, "mcd_result_generation": None,
            "mcd_result_fingerprint": None,
            "pending_event_ids": [], "complete": complete,
        }

    def after(self, object_id="42", generation="g1", fingerprint=None, due=False):
        return {
            "object_id": object_id, "source_input_generation": generation,
            "source_input_fingerprint": fingerprint or "a" * 64,
            "output_fingerprint": "c" * 64,
            "eligible": True, "due": due, "observed_at": 30,
        }

    def cycle(self, operation="segment_update", *, cycle_id="c1", objects=None, pause=None):
        objects = objects or ["42"]
        self.comparator.begin_cycle(
            cycle_id=cycle_id, instance_uid="instance-1", root="/srv/app",
            operation=operation, window_start=100, window_end=200,
            allowlist=objects, pause_start=pause[0] if pause else None,
            pause_end=pause[1] if pause else None,
        )
        rows = [self.pre(object_id=item) for item in objects]
        self.comparator.preflight(cycle_id, rows)
        return rows

    def complete(self, cycle_id="c1", *, pre_rows=None, result_status="ok", return_code=0, after_rows=None):
        pre_rows = pre_rows or [self.pre()]
        after_rows = after_rows or [self.after(row["object_id"], row["input_generation"], row["input_fingerprint"]) for row in pre_rows]
        with self.comparator.native_window(cycle_id, lambda: True) as lease:
            for row in pre_rows:
                self.comparator.record_start(
                    cycle_id, row["object_id"], run_id="run-1", pid=123,
                    lock_path=lease.lock_path, started_at=120,
                    input_generation=row["input_generation"], input_fingerprint=row["input_fingerprint"],
                )
                self.comparator.record_result(
                    cycle_id, row["object_id"], status=result_status, return_code=return_code,
                    processed_input_generation=row["input_generation"],
                    processed_input_fingerprint=row["input_fingerprint"],
                    effect_evidence={"processed_object_id": row["object_id"], "observed": True, "artifact_ref": "local://native/result", "artifact_sha256": "d" * 64},
                    finished_at=130, outcome="test",
                )
        return self.comparator.finalize(cycle_id, after_rows, finished_at=30)

    def test_missed_and_handled_classifications(self):
        self.cycle()
        receipt = self.complete()
        self.assertEqual(receipt["objects"][0]["classification"], "mcd_missed")

        completed_pre = self.pre(decision="completed")
        completed_pre["mcd_start_generation"] = completed_pre["input_generation"]
        completed_pre["mcd_result_generation"] = completed_pre["input_generation"]
        completed_pre["mcd_result_fingerprint"] = completed_pre["input_fingerprint"]
        self.comparator.begin_cycle(cycle_id="c2", instance_uid="instance-1", root="/srv/app", operation="segment_update", window_start=100, window_end=200, allowlist=["42"])
        self.comparator.preflight("c2", [completed_pre])
        with self.comparator.native_window("c2", lambda: True) as lease:
            self.comparator.record_start("c2", "42", run_id="run-2", pid=124, lock_path=lease.lock_path, started_at=120, input_generation="g1", input_fingerprint="a" * 64)
            self.comparator.record_result("c2", "42", status="ok", return_code=0, processed_input_generation="g1", processed_input_fingerprint="a" * 64, effect_evidence={"processed_object_id": "42", "observed": True, "artifact_ref": "local://native/result", "artifact_sha256": "d" * 64}, finished_at=130, outcome="test")
        self.assertEqual(self.comparator.finalize("c2", [self.after()])["objects"][0]["classification"], "mcd_handled")

    def test_error_changed_input_missing_history_and_pause_are_not_missed(self):
        self.cycle()
        self.assertEqual(self.complete(result_status="error", return_code=1)["objects"][0]["classification"], "native_error")

        self.cycle(cycle_id="c2")
        changed = self.after(fingerprint="b" * 64)
        self.assertEqual(self.complete(cycle_id="c2", after_rows=[changed])["objects"][0]["classification"], "unknown")

        self.cycle(cycle_id="c3")
        with self.comparator.native_window("c3", lambda: True):
            pass
        self.assertEqual(self.comparator.cycle_state("c3"), "running")
        self.comparator.abort_cycle("c3", "missing execution evidence")

        self.cycle(cycle_id="c4", pause=(5, 15))
        self.assertEqual(self.complete(cycle_id="c4")["objects"][0]["classification"], "unknown")

    def test_processed_input_proof_is_required_and_lock_is_recorded(self):
        self.cycle()
        with self.comparator.native_window("c1", lambda: True) as lease:
            self.comparator.record_start("c1", "42", run_id="run-1", pid=123, lock_path=lease.lock_path, started_at=120, input_generation="g1", input_fingerprint="a" * 64)
            with self.assertRaises(ComparatorError):
                self.comparator.record_result("c1", "42", status="ok", return_code=0, processed_input_generation="g1", processed_input_fingerprint="b" * 64, effect_evidence={"processed_object_id": "42", "observed": True, "artifact_ref": "local://native/result", "artifact_sha256": "d" * 64}, finished_at=130, outcome="bad proof")

    def test_lock_overlap_and_quiescence_fail_closed(self):
        self.cycle()
        self.comparator.begin_cycle(cycle_id="c2", instance_uid="instance-1", root="/srv/app", operation="segment_update", window_start=300, window_end=400, allowlist=["42"])
        self.comparator.preflight("c2", [self.pre()])
        with self.comparator.native_window("c1", lambda: True):
            with self.assertRaises(ComparatorBusy):
                with self.comparator.native_window("c2", lambda: True):
                    pass

        self.comparator.begin_cycle(cycle_id="c3", instance_uid="instance-1", root="/srv/other", operation="segment_update", window_start=500, window_end=600, allowlist=["42"])
        self.comparator.preflight("c3", [self.pre()])
        with self.assertRaises(ComparatorBusy):
            with self.comparator.native_window("c3", lambda: False):
                pass
        self.assertEqual(self.comparator.cycle_state("c3"), "blocked")

    def test_missing_cycle_receipt_and_tamper_detection(self):
        receipt = missing_cycle_receipt(cycle_id="missing", instance_uid="instance-1", root="/srv/app", operation="campaign_update", window_start=1, window_end=2)
        self.assertEqual(validate_receipt(receipt)["operation"], "campaign_rebuild")
        tampered = json.loads(json.dumps(receipt))
        tampered["root"] = "/srv/other"
        with self.assertRaises(ComparatorError):
            validate_receipt(tampered)

    def test_all_operation_kinds_and_temporal_history_guard(self):
        for index, operation in enumerate(("segment_update", "campaign_rebuild", "campaign_trigger"), start=1):
            cycle_id = f"op-{index}"
            self.cycle(operation=operation, cycle_id=cycle_id)
            receipt = self.complete(cycle_id=cycle_id)
            self.assertEqual(receipt["operation"], operation)

        self.comparator.begin_cycle(cycle_id="bad-time", instance_uid="instance-1", root="/srv/time", operation="segment_update", window_start=100, window_end=200, allowlist=["42"])
        impossible = self.pre()
        impossible["observed_at"] = 101
        with self.assertRaises(ComparatorError):
            self.comparator.preflight("bad-time", [impossible])

    def test_mcd_obligation_gate_rejects_recent_and_incomplete_history(self):
        recent = self.pre()
        recent["obligation_deadline"] = 25
        self.comparator.begin_cycle(cycle_id="recent", instance_uid="instance-1", root="/srv/recent", operation="segment_update", window_start=100, window_end=200, allowlist=["42"])
        self.comparator.preflight("recent", [recent])
        self.assertEqual(self.complete(cycle_id="recent")["objects"][0]["classification"], "unknown")

        incomplete = self.pre()
        incomplete["history_complete_until"] = 12
        self.comparator.begin_cycle(cycle_id="incomplete", instance_uid="instance-1", root="/srv/incomplete", operation="segment_update", window_start=100, window_end=200, allowlist=["42"])
        self.comparator.preflight("incomplete", [incomplete])
        self.assertEqual(self.complete(cycle_id="incomplete")["objects"][0]["classification"], "unknown")

    def test_native_error_does_not_require_success_proof(self):
        self.cycle()
        with self.comparator.native_window("c1", lambda: True) as lease:
            self.comparator.record_start("c1", "42", run_id="failed-run", pid=999, lock_path=lease.lock_path, started_at=120, input_generation="g1", input_fingerprint="a" * 64)
            self.comparator.record_result("c1", "42", status="error", return_code=17, processed_input_generation=None, processed_input_fingerprint=None, effect_evidence={}, finished_at=130, outcome="native failed")
        self.assertEqual(self.comparator.finalize("c1", [self.after()])["objects"][0]["classification"], "native_error")

    def test_executor_runs_all_native_operation_kinds_with_real_pid_and_rc(self):
        for index, operation in enumerate(("segment_update", "campaign_rebuild", "campaign_trigger"), start=1):
            cycle_id = f"exec-{index}"
            row = self.pre()
            now = time.time()
            self.comparator.begin_cycle(cycle_id=cycle_id, instance_uid="instance-1", root="/tmp", operation=operation, window_start=now - 10, window_end=now + 100, allowlist=["42"])
            self.comparator.preflight(cycle_id, [row])
            receipt = execute_native_comparator_cycle(
                comparator=self.comparator,
                cycle_id=cycle_id,
                root="/tmp",
                command=["/bin/sh", "-c", "exit 0"],
                observations=[row],
                after_observer=lambda: [self.after()],
                outcome_observer=lambda object_id: {"processed_input_generation": "g1", "processed_input_fingerprint": "a" * 64, "effect_evidence": {"processed_object_id": object_id, "observed": True, "artifact_ref": "local://fixture", "artifact_sha256": "e" * 64}},
                quiescence_probe=lambda: True,
                instance_uid="instance-1",
                operation=operation,
            )
            self.assertEqual(receipt["operation"], operation)
            self.assertEqual(receipt["objects"][0]["native_result"]["return_code"], 0)
            self.assertGreater(receipt["objects"][0]["native_start"]["pid"], 0)

    def test_executor_without_per_object_proof_is_unknown_and_context_is_bound(self):
        row = self.pre()
        now = time.time()
        self.comparator.begin_cycle(cycle_id="no-proof", instance_uid="instance-1", root="/tmp", operation="segment_update", window_start=now - 10, window_end=now + 100, allowlist=["42"])
        self.comparator.preflight("no-proof", [row])
        receipt = execute_native_comparator_cycle(
            comparator=self.comparator, cycle_id="no-proof", root="/tmp", command=["/bin/sh", "-c", "exit 0"],
            observations=[row], after_observer=lambda: [self.after()], outcome_observer=lambda object_id: {},
            quiescence_probe=lambda: True, instance_uid="instance-1", operation="segment_update",
        )
        self.assertEqual(receipt["objects"][0]["classification"], "unknown")

        self.comparator.begin_cycle(cycle_id="bad-context", instance_uid="instance-1", root="/tmp", operation="segment_update", window_start=now - 10, window_end=now + 100, allowlist=["42"])
        self.comparator.preflight("bad-context", [row])
        with self.assertRaises(ValueError):
            execute_native_comparator_cycle(
                comparator=self.comparator, cycle_id="bad-context", root="/other", command=["/bin/sh", "-c", "exit 0"],
                observations=[row], after_observer=lambda: [self.after()], outcome_observer=lambda object_id: {},
                quiescence_probe=lambda: True, instance_uid="instance-1", operation="segment_update",
            )

    def test_shared_native_fence_rejects_regular_scheduler_admission(self):
        shared = str(Path(self.tmp.name) / "shared-locks")
        os.makedirs(shared, mode=0o755, exist_ok=True)
        root = "/tmp/mcd-fence-test"
        now = time.time()
        comparator = NativeComparator(str(Path(self.tmp.name) / "fence.sqlite"), shared)
        row = self.pre()
        comparator.begin_cycle(cycle_id="fence", instance_uid="instance-1", root=root, operation="segment_update", window_start=now - 10, window_end=now + 100, allowlist=["42"])
        comparator.preflight("fence", [row])
        with comparator.native_window("fence", lambda: True):
            fence_path = shared_root_fence_path(root, shared)
            command = [sys.executable, "-c", "import fcntl, os, sys; fd=os.open(sys.argv[1], os.O_RDWR);\ntry: fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)\nexcept BlockingIOError: raise SystemExit(75)\nraise SystemExit(0)", str(fence_path)]
            result = subprocess.run(command, cwd="/tmp", capture_output=True, timeout=5)
            self.assertEqual(result.returncode, _TASK_LOCK_BUSY_RC)

    def test_empty_native_scope_records_a_bounded_noop_receipt(self):
        now = time.time()
        self.comparator.begin_cycle(cycle_id="empty", instance_uid="instance-1", root="/tmp", operation="campaign_trigger", window_start=now - 10, window_end=now + 100, allowlist=[])
        self.comparator.preflight("empty", [])
        receipt = execute_native_comparator_cycle(
            comparator=self.comparator, cycle_id="empty", root="/tmp", command=["/bin/sh", "-c", "exit 0"],
            observations=[], after_observer=lambda: [], outcome_observer=lambda object_id: {},
            quiescence_probe=lambda: True, instance_uid="instance-1", operation="campaign_trigger",
        )
        self.assertEqual(receipt["objects"], [])
        self.assertEqual(receipt["status"], "complete")

    def test_completed_mcd_path_is_explicit_for_all_operation_kinds(self):
        for index, operation in enumerate(("segment_update", "campaign_rebuild", "campaign_trigger"), start=1):
            cycle_id = f"mcd-complete-{index}"
            row = self.pre(decision="completed")
            row["mcd_start_generation"] = row["input_generation"]
            row["mcd_result_generation"] = row["input_generation"]
            row["mcd_result_fingerprint"] = row["input_fingerprint"]
            self.comparator.begin_cycle(cycle_id=cycle_id, instance_uid="instance-1", root="/tmp", operation=operation, window_start=100, window_end=200, allowlist=["42"], eligible_denominator=1, capture_limit=1, coverage="complete")
            self.comparator.preflight(cycle_id, [row])
            with self.comparator.native_window(cycle_id, lambda: True) as lease:
                self.comparator.record_start(cycle_id, "42", run_id=f"mcd-{index}", pid=1000 + index, lock_path=lease.lock_path, started_at=120, input_generation="g1", input_fingerprint="a" * 64)
                self.comparator.record_result(cycle_id, "42", status="ok", return_code=0, processed_input_generation="g1", processed_input_fingerprint="a" * 64, effect_evidence={"processed_object_id":"42","observed":True,"artifact_ref":"local://mcd","artifact_sha256":"f" * 64}, finished_at=130, outcome="mcd_completed")
            receipt = self.comparator.finalize(cycle_id, [self.after()])
            self.assertEqual(receipt["objects"][0]["classification"], "mcd_handled")


if __name__ == "__main__":
    unittest.main()
