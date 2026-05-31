from __future__ import annotations

import unittest
from types import SimpleNamespace

from mcd_agent.daemon import _backup_storage_probe_allowed


def _cfg(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "backup_enabled": True,
        "backup_cluster_enabled": False,
        "backup_cluster_remote_enabled": True,
        "backup_cluster_authority_role": "replica",
        "backup_cluster_authority_host": "",
        "cluster_route_backup_host": "",
        "cluster_id": "cluster-test",
        "cluster_name": "cluster-test",
        "cluster_node_role": "node",
        "cluster_node_index": 1,
        "mcc_host_name": "node-1",
        "backup_instance_name": "",
        "backup_host_name": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class BackupStorageProbeGuardTests(unittest.TestCase):
    def test_standalone_backup_host_can_probe(self) -> None:
        allowed, reason = _backup_storage_probe_allowed(_cfg(backup_cluster_enabled=False))

        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_cluster_non_authority_node_skips_probe(self) -> None:
        allowed, reason = _backup_storage_probe_allowed(
            _cfg(backup_cluster_enabled=True, cluster_node_role="node")
        )

        self.assertFalse(allowed)
        self.assertIn("cluster backup non-authority", reason)

    def test_cluster_replica_authority_can_probe(self) -> None:
        allowed, reason = _backup_storage_probe_allowed(
            _cfg(backup_cluster_enabled=True, cluster_node_role="replica")
        )

        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_cluster_authority_skips_when_remote_backup_disabled(self) -> None:
        allowed, reason = _backup_storage_probe_allowed(
            _cfg(
                backup_cluster_enabled=True,
                cluster_node_role="replica",
                backup_cluster_remote_enabled=False,
            )
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "cluster remote backup disabled")


if __name__ == "__main__":
    unittest.main()
