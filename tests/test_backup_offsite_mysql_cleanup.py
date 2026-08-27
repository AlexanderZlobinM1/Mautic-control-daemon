from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.backup import (
    _apply_cluster_backup_integrity_status,
    _cluster_backup_integrity_problem,
    _cluster_full_required_free_bytes,
    _cluster_file_source_path_forbidden,
    _cluster_file_source_paths,
    _cluster_prepared_mysql_datadir_from_cmdline,
    _cleanup_stale_prepared_mysql_processes,
    _prepared_mysql_has_active_clients,
    _run_mydumper_from_xtrabackup_full,
    cluster_backup_status,
)
from mcd_agent.models import DBConfig


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(backup_cluster_local_root_dir="/mnt/data/backup/local/ananasrs", backup_dump_timeout_sec=43200)


class PreparedOffsiteMysqlDetectionTest(unittest.TestCase):
    def test_full_space_requirement_uses_database_size_and_headroom(self) -> None:
        cfg = SimpleNamespace(backup_cluster_incremental_min_free_bytes=300)
        db = DBConfig(host="localhost", port=3306, name="baza_ananas", user="backup", password="x", table_prefix="")

        with patch("mcd_agent.backup._mysql_capture", return_value="1000"):
            required = _cluster_full_required_free_bytes(cfg, db)

        self.assertEqual(required, 1250)

    def test_full_space_requirement_keeps_configured_headroom(self) -> None:
        cfg = SimpleNamespace(backup_cluster_incremental_min_free_bytes=2000)
        db = DBConfig(host="localhost", port=3306, name="baza_ananas", user="backup", password="x", table_prefix="")

        with patch("mcd_agent.backup._mysql_capture", return_value="1000"):
            required = _cluster_full_required_free_bytes(cfg, db)

        self.assertEqual(required, 2000)

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

    def test_detects_local_full_read_only_offsite_mysql(self) -> None:
        cmdline = (
            "/usr/sbin/mysqld --no-defaults "
            "--datadir=/mnt/data/backup/local/ananasrs/db/full-20260718-092355/physical-xtrabackup "
            "--socket=/tmp/mcd-offsite-mysql-r6uxlec_/mysql.sock "
            "--skip-networking --skip-log-bin --skip-grant-tables --read-only=ON --super-read-only=ON"
        )

        datadir = _cluster_prepared_mysql_datadir_from_cmdline(_cfg(), cmdline)

        self.assertEqual(
            datadir,
            Path("/mnt/data/backup/local/ananasrs/db/full-20260718-092355/physical-xtrabackup"),
        )

    def test_mydumper_uses_local_full_without_offsite_staging(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            full_dir = Path(td) / "full-20260718-092355"
            source = full_dir / "physical-xtrabackup"
            source.mkdir(parents=True)
            (source / "xtrabackup_checkpoints").write_text("backup_type = full-prepared\n", encoding="utf-8")
            (source / "ibdata1").write_bytes(b"data")
            output_dir = Path(td) / "dump"
            cfg = SimpleNamespace(backup_dump_timeout_sec=30, backup_mount_timeout_sec=30)
            db = DBConfig(host="localhost", port=3306, name="baza_ananas", user="backup", password="x", table_prefix="")
            runtime = SimpleNamespace(socket_path=Path("/tmp/mcd-offsite-test.sock"))

            with (
                patch("mcd_agent.backup._prepare_xtrabackup_full_for_mysql", return_value=source) as prepare,
                patch("mcd_agent.backup._start_prepared_xtrabackup_mysql", return_value=runtime),
                patch("mcd_agent.backup._stop_prepared_xtrabackup_mysql"),
                patch("mcd_agent.backup.replace", side_effect=lambda obj, **changes: obj),
                patch("mcd_agent.backup._run_mydumper") as dump,
            ):
                meta = _run_mydumper_from_xtrabackup_full(cfg, db, full_dir, output_dir)

            prepare.assert_called_once_with(cfg, full_dir)
            dump.assert_called_once()
            self.assertEqual(meta["offsite_temp_mysql"], "local_full_read_only")
            self.assertFalse((Path(td) / "offsite-mysql").exists())

    def test_cleanup_keeps_fresh_existing_prepared_mysql(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            datadir = Path(td) / "prepared-20260705-010000"
            datadir.mkdir()

            with (
                patch(
                    "mcd_agent.backup._cluster_prepared_mysql_processes",
                    return_value=[{"pid": 123, "datadir": datadir, "age_sec": 60}],
                ),
                patch("mcd_agent.backup._terminate_pid") as terminate,
            ):
                stopped = _cleanup_stale_prepared_mysql_processes(_cfg())

        self.assertEqual(stopped, [])
        terminate.assert_not_called()

    def test_cleanup_stops_too_old_existing_prepared_mysql(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            datadir = Path(td) / "prepared-20260701-012026"
            datadir.mkdir()

            with (
                patch(
                    "mcd_agent.backup._cluster_prepared_mysql_processes",
                    return_value=[{"pid": 1495368, "datadir": datadir, "age_sec": 4 * 24 * 3600}],
                ),
                patch("mcd_agent.backup._terminate_pid", return_value=True) as terminate,
            ):
                stopped = _cleanup_stale_prepared_mysql_processes(_cfg())

        self.assertEqual(stopped, [1495368])
        terminate.assert_called_once_with(1495368)

    def test_cleanup_stops_idle_prepared_mysql_after_grace_period(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            datadir = Path(td) / "full-20260720-213347" / "physical-xtrabackup"
            datadir.mkdir(parents=True)
            item = {
                "pid": 3940850,
                "datadir": datadir,
                "age_sec": 2 * 3600,
                "cmdline": "mysqld --socket=/tmp/mcd-offsite-mysql-test/mysql.sock",
            }

            with (
                patch("mcd_agent.backup._cluster_prepared_mysql_processes", return_value=[item]),
                patch("mcd_agent.backup._prepared_mysql_has_active_clients", return_value=False),
                patch("mcd_agent.backup._terminate_pid", return_value=True) as terminate,
            ):
                stopped = _cleanup_stale_prepared_mysql_processes(_cfg())

        self.assertEqual(stopped, [3940850])
        terminate.assert_called_once_with(3940850)

    def test_cleanup_keeps_prepared_mysql_with_active_dump_client(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            datadir = Path(td) / "full-20260720-213347" / "physical-xtrabackup"
            datadir.mkdir(parents=True)
            item = {
                "pid": 3940851,
                "datadir": datadir,
                "age_sec": 2 * 3600,
                "cmdline": "mysqld --socket=/tmp/mcd-offsite-mysql-test/mysql.sock",
            }

            with (
                patch("mcd_agent.backup._cluster_prepared_mysql_processes", return_value=[item]),
                patch("mcd_agent.backup._prepared_mysql_has_active_clients", return_value=True),
                patch("mcd_agent.backup._terminate_pid") as terminate,
            ):
                stopped = _cleanup_stale_prepared_mysql_processes(_cfg())

        self.assertEqual(stopped, [])
        terminate.assert_not_called()


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

    def test_unmounted_offsite_mount_keeps_persisted_archive_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            state_dir = base / "state"
            state_dir.mkdir()
            local_root = base / "local"
            local_root.mkdir()
            mount_base = base / "mounts"
            host_name = "ananas-cluster-replica-xtrabackup"
            offsite_archive = (
                mount_base
                / host_name
                / "backup"
                / "ananasrs.sales-snap.com"
                / "daily"
                / "2026-06-08"
                / "files-snapshot-20260608-011740.tar.gz"
            )
            (state_dir / f"host-{host_name}.json").write_text(
                json.dumps(
                    {
                        "last_status": "ok",
                        "last_error": "",
                        "last_backup_kind": "cluster_offsite",
                        "last_offsite_backup_path": str(offsite_archive.parent),
                        "last_offsite_files_archive_path": str(offsite_archive),
                        "last_offsite_files_archive_ok": True,
                    }
                ),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(
                backup_host_name=host_name,
                backup_state_dir=str(state_dir),
                backup_mount_base_dir=str(mount_base),
                backup_cluster_enabled=True,
                backup_cluster_local_root_dir=str(local_root),
                backup_lock_dir=str(base / "locks"),
                cluster_id="cluster-ananasrs-prod",
                cluster_name="ananasrs.sales-snap.com",
                cluster_node_role="replica",
                cluster_node_index=6,
                backup_cluster_authority_role="replica",
                backup_cluster_authority_host="",
                cluster_route_backup_host="",
            )

            with (
                patch("mcd_agent.backup._effective_cfg", side_effect=lambda x: x),
                patch("mcd_agent.backup._mounted", return_value=False),
                patch("mcd_agent.backup._cluster_offsite_processes", return_value=[]),
            ):
                state = cluster_backup_status(cfg)

        self.assertEqual(state["last_status"], "ok")
        self.assertEqual(state["last_error"], "")
        self.assertEqual(state["cluster_integrity_status"], "ok")
        self.assertTrue(state["last_offsite_files_archive_ok"])

    def test_status_recovers_completed_offsite_marker_after_partial_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            state_dir = base / "state"
            state_dir.mkdir()
            local_root = base / "local"
            local_root.mkdir()
            mount_base = base / "mounts"
            host_name = "ananas-cluster-replica-xtrabackup"
            cluster_name = "ananasrs.sales-snap.com"
            daily = mount_base / host_name / "backup" / cluster_name / "daily"
            final_dir = daily / "2026-07-05"
            final_dir.mkdir(parents=True)
            archive = final_dir / "files-snapshot-20260705-213454.tar.gz"
            archive.write_bytes(b"snapshot")
            stale_archive = daily / ".incomplete-2026-07-05-20260705-214005" / archive.name
            (final_dir / ".mcd-backup.json").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "ts_utc": "2026-07-05T23:32:37+00:00",
                        "cluster_backup": True,
                        "cluster_name": cluster_name,
                        "host_name": host_name,
                        "method": "mydumper",
                        "path": str(final_dir),
                        "files_archive_path": str(stale_archive),
                        "bytes_written": 115360599358,
                        "db_bytes": 115152079456,
                        "files_bytes": len(b"snapshot"),
                    }
                ),
                encoding="utf-8",
            )
            state_file = state_dir / f"host-{host_name}.json"
            state_file.write_text(
                json.dumps(
                    {
                        "last_status": "running",
                        "last_error": "",
                        "job": "backup.cluster.offsite",
                        "last_run_at": "2026-07-05T21:34:57+00:00",
                        "last_success_at": "2026-07-05T21:34:56+00:00",
                        "last_offsite_backup_at": "2026-06-30T02:44:36+00:00",
                        "last_offsite_backup_path": str(daily / "2026-06-30"),
                    }
                ),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(
                backup_host_name=host_name,
                backup_state_dir=str(state_dir),
                backup_mount_base_dir=str(mount_base),
                backup_remote_root_dir="backup",
                backup_cluster_enabled=True,
                backup_cluster_local_root_dir=str(local_root),
                backup_lock_dir=str(base / "locks"),
                cluster_id="cluster-ananasrs-prod",
                cluster_name=cluster_name,
                cluster_node_role="replica",
                cluster_node_index=6,
                backup_cluster_authority_role="replica",
                backup_cluster_authority_host="",
                cluster_route_backup_host="",
            )

            with (
                patch("mcd_agent.backup._effective_cfg", side_effect=lambda x: x),
                patch("mcd_agent.backup._mounted", return_value=True),
                patch("mcd_agent.backup._cluster_offsite_processes", return_value=[]),
            ):
                state = cluster_backup_status(cfg)

            self.assertEqual(state["last_status"], "ok")
            self.assertEqual(state["last_offsite_backup_at"], "2026-07-05T23:32:37+00:00")
            self.assertEqual(state["last_offsite_backup_path"], str(final_dir))
            self.assertEqual(state["last_offsite_files_archive_path"], str(archive))
            self.assertTrue(state["last_offsite_files_archive_ok"])
            persisted = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["last_status"], "ok")
            self.assertEqual(persisted["last_backup_kind"], "cluster_offsite")

    def test_status_does_not_probe_remote_tree_while_offsite_backup_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            state_dir = base / "state"
            state_dir.mkdir()
            local_root = base / "local"
            local_root.mkdir()
            mount_base = base / "mounts"
            host_name = "ananas-cluster-replica-xtrabackup"
            state_file = state_dir / f"host-{host_name}.json"
            state_file.write_text(
                json.dumps(
                    {
                        "last_status": "running",
                        "last_error": "",
                        "job": "backup.cluster.offsite",
                        "last_run_at": "2026-07-21T00:05:09+00:00",
                        "last_offsite_backup_path": str(mount_base / host_name / "backup" / "ananasrs.sales-snap.com" / "daily" / "2026-07-20"),
                    }
                ),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(
                backup_host_name=host_name,
                backup_state_dir=str(state_dir),
                backup_mount_base_dir=str(mount_base),
                backup_cluster_enabled=True,
                backup_cluster_local_root_dir=str(local_root),
                backup_lock_dir=str(base / "locks"),
                cluster_id="cluster-ananasrs-prod",
                cluster_name="ananasrs.sales-snap.com",
                cluster_node_role="replica",
                cluster_node_index=6,
                backup_cluster_authority_role="replica",
                backup_cluster_authority_host="",
                cluster_route_backup_host="",
            )

            with (
                patch("mcd_agent.backup._effective_cfg", side_effect=lambda x: x),
                patch("mcd_agent.backup._mounted", return_value=True),
                patch("mcd_agent.backup._cluster_offsite_processes", return_value=[4321]),
                patch("mcd_agent.backup._cluster_recover_offsite_state_from_remote") as recover,
                patch("mcd_agent.backup._read_backup_marker") as marker,
            ):
                state = cluster_backup_status(cfg)

        self.assertTrue(state["offsite_running"])
        self.assertEqual(state["active_offsite_pids"], [4321])
        self.assertEqual(state["offsite_status_probe"], "skipped_active_backup")
        recover.assert_not_called()
        marker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
