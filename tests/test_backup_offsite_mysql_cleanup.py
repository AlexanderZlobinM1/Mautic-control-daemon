from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from mcd_agent.backup import (
    _apply_cluster_backup_integrity_status,
    _cluster_backup_integrity_problem,
    _cluster_file_source_path_forbidden,
    _cluster_file_source_paths,
    _cluster_prepared_mysql_datadir_from_cmdline,
)


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(backup_cluster_local_root_dir="/mnt/data/backup/local/ananasrs")


class PreparedOffsiteMysqlDetectionTest(unittest.TestCase):
    def test_detects_mcd_prepared_offsite_mysql(self) -> None:
        cmdline = (
            "/usr/sbin/mysqld --no-defaults "
            "--datadir=/mnt/data/backup/local/ananasrs/db/offsite-mysql/prepared-20260513-093830 "
            "--socket=/tmp/mcd-offsite-mysql-r6uxlec_/mysql.sock "
            "--skip-networking --skip-log-bin --skip-grant-tables --read-only=ON --super-read-only=ON"
        )

        datadir = _cluster_prepared_mysql_datadir_from_cmdline(_cfg(), cmdline)

        self.assertEqual(
            datadir,
            Path("/mnt/data/backup/local/ananasrs/db/offsite-mysql/prepared-20260513-093830"),
        )

    def test_rejects_production_mysql(self) -> None:
        cmdline = "/usr/sbin/mysqld --datadir=/var/lib/mysql --socket=/run/mysqld/mysqld.sock"

        self.assertIsNone(_cluster_prepared_mysql_datadir_from_cmdline(_cfg(), cmdline))

    def test_rejects_non_mcd_socket_even_under_backup_root(self) -> None:
        cmdline = (
            "/usr/sbin/mysqld --no-defaults "
            "--datadir=/mnt/data/backup/local/ananasrs/db/offsite-mysql/prepared-20260513-093830 "
            "--socket=/tmp/mysql.sock --skip-networking --skip-grant-tables"
        )

        self.assertIsNone(_cluster_prepared_mysql_datadir_from_cmdline(_cfg(), cmdline))


class ClusterFileSourcePathsTest(unittest.TestCase):
    def test_rejects_runtime_and_gluster_paths(self) -> None:
        for path in (
            "/var/lib/glusterd",
            "/var/lib/glusterd/vols/media",
            "/var/lib/mysql",
            "/var/log",
            "/root/.ssh/id_ed25519",
            "/run",
            "/mnt",
        ):
            with self.subTest(path=path):
                self.assertTrue(_cluster_file_source_path_forbidden(path))

    def test_filters_forbidden_paths_from_config(self) -> None:
        cfg = SimpleNamespace(
            backup_cluster_files_node_paths=[
                "/var/lib/glusterd",
                "/var/lib/mysql",
                "/etc",
            ]
        )

        paths = _cluster_file_source_paths(cfg, "backup_cluster_files_node_paths")

        self.assertNotIn("/var/lib/glusterd", paths)
        self.assertNotIn("/var/lib/mysql", paths)
        self.assertIn("/etc", paths)


class ClusterBackupIntegrityStatusTest(unittest.TestCase):
    def test_missing_offsite_files_archive_marks_status_failed(self) -> None:
        state = {
            "last_status": "ok",
            "last_error": "",
            "last_offsite_files_archive_path": "/backup/cluster/daily/files.tar.zst",
            "last_offsite_files_archive_ok": False,
        }

        _apply_cluster_backup_integrity_status(state)

        self.assertEqual(state["cluster_integrity_status"], "failed")
        self.assertEqual(state["last_status"], "failed")
        self.assertIn("last offsite files archive missing", state["last_error"])

    def test_absent_offsite_archive_path_does_not_fail_standalone_status(self) -> None:
        state = {"last_status": "ok", "last_offsite_files_archive_ok": False}

        _apply_cluster_backup_integrity_status(state)

        self.assertEqual(state["cluster_integrity_status"], "ok")
        self.assertEqual(state["last_status"], "ok")
        self.assertEqual(_cluster_backup_integrity_problem(state), "")


if __name__ == "__main__":
    unittest.main()
