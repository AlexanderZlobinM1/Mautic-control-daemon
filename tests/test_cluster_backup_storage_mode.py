from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.backup import _cluster_direct_storage_cfg, _cluster_release_direct_storage


class ClusterBackupStorageModeTests(unittest.TestCase):
    def _cfg(self, root: str) -> SimpleNamespace:
        return SimpleNamespace(
            backup_cluster_storage_mode="sshfs",
            backup_cluster_remote_enabled=True,
            backup_cluster_enabled=True,
            backup_cluster_authority_role="replica",
            backup_cluster_authority_host="",
            cluster_node_role="replica",
            cluster_node_index=0,
            cluster_id="cluster-ananasrs",
            cluster_name="ananasrs.sales-snap.com",
            backup_mount_base_dir=root,
            backup_remote_root_dir="backup",
            backup_host_name="ananas-cluster-replica-xtrabackup",
            backup_ssh_host="storagebox.example",
            backup_ssh_user="storage",
            backup_ssh_key_file="/root/.ssh/storagebox",
            backup_ssh_password="",
            backup_sshfs_package="sshfs",
            backup_unmount_timeout_sec=10,
        )

    def test_direct_mode_maps_cluster_root_under_sshfs_mount(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td)
            with patch("mcd_agent.backup._validate_cluster_cfg"), patch(
                "mcd_agent.backup._ensure_cluster_backup_authority"
            ), patch("mcd_agent.backup._ensure_cluster_tools"), patch(
                "mcd_agent.backup._mount"
            ), patch("mcd_agent.backup._mounted", return_value=True), patch(
                "mcd_agent.backup.replace",
                side_effect=lambda obj, **changes: SimpleNamespace(**{**vars(obj), **changes}),
            ):
                mapped, mount_path = _cluster_direct_storage_cfg(cfg)

            self.assertEqual(mount_path, Path(td) / "ananas-cluster-replica-xtrabackup")
            self.assertEqual(
                mapped.backup_cluster_local_root_dir,
                str(mount_path / "backup" / "ananasrs.sales-snap.com" / "local"),
            )

    def test_release_unmounts_direct_mode_mount(self) -> None:
        mount_path = Path("/tmp/mcd-storagebox-test")
        cfg = SimpleNamespace(backup_unmount_timeout_sec=10)
        with patch("mcd_agent.backup._unmount") as unmount:
            _cluster_release_direct_storage(cfg, mount_path)
        unmount.assert_called_once_with(mount_path, 10)


if __name__ == "__main__":
    unittest.main()
