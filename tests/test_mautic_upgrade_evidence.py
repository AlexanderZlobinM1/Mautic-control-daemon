from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from mcd_agent.mautic_patch_resolution import MauticPatchEvidencePending, MauticPatchResolutionError
from mcd_agent.mautic_upgrade import _report_upgrade_patch_evidence
from mcd_agent.mautic_upgrade_evidence import (
    deliver_upgrade_evidence,
    enqueue,
    status,
)


def _request(tmp_path: Path) -> dict[str, object]:
    return {
        "schema": "mcd-mautic-patch-evidence-v1",
        "mcc_host_name": "mcc-host",
        "hostname": "agent.example",
        "host_id": "host-id",
        "instance_uid": "instance-1",
        "catalog_revision": "catalog-1",
        "catalog_sha256": "a" * 64,
        "plan_sha256": "b" * 64,
        "trigger": "upgrade_lifecycle",
        "phase": "dependency_update_preflight",
        "run_id": "upgrade-1",
        "source_version": "7.2.0",
        "target_version": "7.2.1",
        "evidence": {"schema": "mcd-mautic-patch-evidence-v1", "status": "success"},
    }


class UpgradeEvidenceOutboxTests(unittest.TestCase):
    def test_ack_is_required_before_delivery_returns(self) -> None:
        request = _request(Path("."))
        config = SimpleNamespace(
            state_db_path=str(self._tmp / "state.db"),
            mcc_url="https://mcc.example",
            mcc_token="token",
        )
        response = {"schema": "mcc-mautic-patch-evidence-accepted-v1", "status": "accepted", "evidence_id": "e1"}
        with patch("mcd_agent.mautic_patch_resolution.report_evidence", return_value=response) as report:
            accepted = deliver_upgrade_evidence(config, request)
        self.assertEqual(accepted, response)
        self.assertEqual(report.call_count, 1)
        row = status(config.state_db_path, enqueue(config.state_db_path, request))
        self.assertEqual(row["status"], "accepted")

    def test_delayed_ack_retries_same_request_without_replaying_upgrade(self) -> None:
        request = _request(self._tmp)
        config = SimpleNamespace(state_db_path=str(self._tmp / "state.db"), mcc_url="https://mcc.example", mcc_token="token")
        response = {"schema": "mcc-mautic-patch-evidence-accepted-v1", "status": "accepted", "evidence_id": "e1"}
        with patch(
            "mcd_agent.mautic_patch_resolution.report_evidence",
            side_effect=[MauticPatchEvidencePending("mcc_patch_evidence_pending"), response],
        ) as report:
            self.assertEqual(deliver_upgrade_evidence(config, request), response)
        self.assertEqual(report.call_count, 2)
        self.assertEqual(report.call_args_list[0].kwargs, report.call_args_list[1].kwargs)

    def test_plan_mismatch_is_blocked_and_never_retried(self) -> None:
        request = _request(self._tmp)
        config = SimpleNamespace(state_db_path=str(self._tmp / "state.db"), mcc_url="https://mcc.example", mcc_token="token")
        with patch(
            "mcd_agent.mautic_patch_resolution.report_evidence",
            side_effect=MauticPatchResolutionError("mcc_patch_http_409:immutable_plan_mismatch"),
        ) as report:
            with self.assertRaisesRegex(Exception, "immutable_plan_mismatch"):
                deliver_upgrade_evidence(config, request)
        self.assertEqual(report.call_count, 1)
        row = status(config.state_db_path, enqueue(config.state_db_path, request))
        self.assertEqual(row["status"], "blocked")

    def test_terminal_receipt_contains_exact_guard_and_v3_identity(self) -> None:
        plan = {
            "schema": "mcd-mautic-patch-plan-v3",
            "run_id": "upgrade-1",
            "source_version": "7.2.0",
            "target_version": "7.2.1",
            "trigger": "upgrade_lifecycle",
            "phase": "dependency_update_preflight",
            "registry_commit": "c" * 40,
            "registry_sha256": "d" * 64,
            "patches": [],
        }
        resolution = {
            "host_id": "host-id",
            "catalog_revision": "catalog-1",
            "catalog_sha256": "a" * 64,
            "plan_sha256": "b" * 64,
        }
        config = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
        install = SimpleNamespace(instance_uid="instance-1")
        with patch("mcd_agent.mautic_patch_resolution._host_identity", return_value=("mcc-host", "agent.example")), \
             patch("mcd_agent.mautic_patch_resolution.report_evidence", return_value={"status": "accepted", "evidence_id": "e1"}) as report:
            self.assertTrue(_report_upgrade_patch_evidence(
                config, install, resolution, plan,
                started_at="2026-11-22T00:00:00+00:00", status="success",
                source_version="7.2.0", target_version="7.2.1",
                maintenance_guard={"cron_restored": True, "scheduler_resumed": True},
            ))
        receipt = report.call_args.kwargs["evidence"]["upgrade_receipt"]
        self.assertEqual(receipt["schema"], "mcd-mautic-upgrade-terminal-receipt-v1")
        self.assertEqual(receipt["source_version"], "7.2.0")
        self.assertEqual(receipt["target_version"], "7.2.1")
        self.assertEqual(receipt["run_id"], "upgrade-1")
        self.assertEqual(receipt["plan_sha256"], "b" * 64)
        self.assertEqual(receipt["evidence_identity"]["registry_commit"], "c" * 40)
        self.assertEqual(receipt["maintenance_guard"], {"cron_restored": True, "scheduler_resumed": True})

    def setUp(self) -> None:
        import tempfile

        self._tmp = Path(tempfile.mkdtemp(prefix="mcd-upgrade-evidence-"))


if __name__ == "__main__":
    unittest.main()
