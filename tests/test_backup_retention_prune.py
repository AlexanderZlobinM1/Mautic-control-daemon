from __future__ import annotations

import json
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path

from mcd_agent.backup import (
    _cluster_remote_retention_plan_for_parent,
    _is_within_parent,
    _backup_retention_candidate_dirs,
    _instance_backup_retention_enabled,
    _prune_by_copies,
    _write_host_backup_instance_manifests,
)
from mcd_agent.models import DBConfig, MauticInstall


class BackupRetentionPruneTests(unittest.TestCase):
    def test_retention_candidates_include_dates_and_instance_timestamps_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            for name in ("2026-06-19", "20260620-010203", "notes", ".incomplete-20260621-010203"):
                (parent / name).mkdir()
            (parent / "file.txt").write_text("not a backup\n", encoding="utf-8")

            names = [p.name for p in _backup_retention_candidate_dirs(parent)]

        self.assertEqual(names, ["20260620-010203", "2026-06-19"])

    def test_cluster_remote_retention_plan_handles_superseded_date_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            daily = parent / "daily"
            daily.mkdir()
            for name in ("2026-06-20", "2026-06-21", ".superseded-live-2026-06-19"):
                (daily / name).mkdir()
            (parent / ".superseded-live-2026-06-18").mkdir()

            for name in ("2026-06-14",):
                (daily / name).mkdir()

            plan = _cluster_remote_retention_plan_for_parent(
                parent,
                keep_daily=2,
                keep_weekly=0,
                apply=False,
            )

            self.assertEqual(plan["problems"], [])
            self.assertCountEqual(
                [entry["path"] for entry in plan["kept"]],
                ["daily/2026-06-21", "daily/2026-06-20"],
            )
            self.assertCountEqual(
                [entry["path"] for entry in plan["delete_candidates"]],
                [".superseded-live-2026-06-18", "daily/.superseded-live-2026-06-19", "daily/2026-06-14"],
            )

    def test_cluster_remote_retention_plan_reports_non_date_dirs_as_problem(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            daily = parent / "daily"
            daily.mkdir()
            for name in ("2026-06-14", "legacy-no-date"):
                (daily / name).mkdir()

            plan = _cluster_remote_retention_plan_for_parent(
                parent,
                keep_daily=1,
                keep_weekly=0,
                apply=False,
            )

            self.assertEqual(len(plan["problems"]), 1)
            self.assertIn("legacy-no-date", plan["problems"][0]["path"])

    def test_cluster_remote_retention_plan_ignores_superseded_do_not_prune_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            superseded = parent / "daily"
            superseded.mkdir()
            for name in ("2026-06-30", ".superseded-live-2026-06-20"):
                (superseded / name).mkdir()

            (superseded / ".superseded-live-2026-06-20" / ".mcd-backup.json").write_text(
                json.dumps({"status": "ok", "do_not_prune": True}),
                encoding="utf-8",
            )

            plan = _cluster_remote_retention_plan_for_parent(
                parent,
                keep_daily=1,
                keep_weekly=0,
                apply=False,
            )

            self.assertCountEqual(
                [entry["path"] for entry in plan["delete_candidates"]],
                ["daily/.superseded-live-2026-06-20"],
            )
            self.assertFalse(plan["protected"])

    def test_is_within_parent_guard_refuses_outside_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            (parent / "daily").mkdir()

            self.assertTrue(
                _is_within_parent(parent, parent / "daily"),
                "_is_within_parent should allow child paths inside parent",
            )
            self.assertFalse(
                _is_within_parent(parent, Path("/")),
                "_is_within_parent should reject filesystem root",
            )
            self.assertFalse(
                _is_within_parent(parent, Path("/tmp")),
                "_is_within_parent should reject external absolute paths",
            )

    def test_deleted_instances_instance_backups_are_manual_delete_only(self) -> None:
        cfg = SimpleNamespace(backup_remote_root_dir="backup")

        self.assertTrue(_instance_backup_retention_enabled(cfg, None))  # type: ignore[arg-type]
        self.assertTrue(_instance_backup_retention_enabled(cfg, "backup"))  # type: ignore[arg-type]
        self.assertFalse(_instance_backup_retention_enabled(cfg, "mcc/deleted-instances"))  # type: ignore[arg-type]
        self.assertFalse(_instance_backup_retention_enabled(cfg, "mcc/deleted-instances/zepter"))  # type: ignore[arg-type]
        self.assertFalse(_instance_backup_retention_enabled(cfg, "/archive/deleted-instances/"))  # type: ignore[arg-type]

    def test_persisted_deleted_instances_root_is_manual_delete_only(self) -> None:
        cfg = SimpleNamespace(backup_remote_root_dir="mcc/deleted-instances")

        self.assertFalse(_instance_backup_retention_enabled(cfg, None))  # type: ignore[arg-type]

    def test_prune_keeps_protected_current_backup_and_removes_old_index_entries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mount = Path(td)
            parent = mount / "backup" / "host-a" / "instances" / "site-a"
            parent.mkdir(parents=True)
            names = [
                "20260618-010000",
                "20260619-010000",
                "20260620-010000",
                "20260621-010000",
            ]
            for name in names:
                backup_dir = parent / name
                backup_dir.mkdir()
                (backup_dir / "mcc-backup-manifest.json").write_text("{}\n", encoding="utf-8")
            index_dir = mount / "mcc-backups-index.d"
            index_dir.mkdir()
            for name in names:
                rel = f"backup/host-a/instances/site-a/{name}"
                (index_dir / f"{name}.json").write_text(
                    json.dumps(
                        {
                            "backup_dir": rel,
                            "manifest_path": f"{rel}/mcc-backup-manifest.json",
                        },
                        ensure_ascii=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )

            removed = _prune_by_copies(
                parent,
                2,
                protected={parent / "20260621-010000"},
                mount_path=mount,
            )

            self.assertEqual(removed, ["20260619-010000", "20260618-010000"])
            self.assertTrue((parent / "20260621-010000").exists())
            self.assertTrue((parent / "20260620-010000").exists())
            self.assertFalse((parent / "20260619-010000").exists())
            self.assertFalse((parent / "20260618-010000").exists())
            self.assertTrue((index_dir / "20260621-010000.json").exists())
            self.assertTrue((index_dir / "20260620-010000.json").exists())
            self.assertFalse((index_dir / "20260619-010000.json").exists())
            self.assertFalse((index_dir / "20260618-010000.json").exists())

    def test_prune_removes_host_backup_instance_sidecars_and_index_entries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mount = Path(td)
            parent = mount / "backup" / "host-a"
            for name in ("2026-06-18", "2026-06-19", "2026-06-20"):
                (parent / name).mkdir(parents=True)
                sidecar = parent / "instances" / "site-a.example.com" / name
                sidecar.mkdir(parents=True)
                (sidecar / "mcc-backup-manifest.json").write_text("{}\n", encoding="utf-8")
            index_dir = mount / "mcc-backups-index.d"
            index_dir.mkdir()
            old_rel = "backup/host-a/instances/site-a.example.com/2026-06-18"
            (index_dir / "old-sidecar.json").write_text(
                json.dumps({"backup_dir": old_rel, "manifest_path": f"{old_rel}/mcc-backup-manifest.json"}) + "\n",
                encoding="utf-8",
            )

            removed = _prune_by_copies(parent, 2, protected={parent / "2026-06-20"}, mount_path=mount)

            self.assertEqual(removed, ["2026-06-18"])
            self.assertFalse((parent / "2026-06-18").exists())
            self.assertFalse((parent / "instances" / "site-a.example.com" / "2026-06-18").exists())
            self.assertFalse((index_dir / "old-sidecar.json").exists())

    def test_host_backup_writes_per_instance_sidecar_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mount = Path(td)
            backup_dir = mount / "backup" / "host-a" / "2026-06-28"
            db_dir = backup_dir / "databases" / "site-a__baza_site_a"
            db_dir.mkdir(parents=True)
            (db_dir / "metadata").write_text("ok\n", encoding="utf-8")
            (db_dir / "table.sql.gz").write_text("sql\n", encoding="utf-8")
            (backup_dir / "files.tar.gz").write_text("files\n", encoding="utf-8")
            marker = {
                "status": "ok",
                "ts_utc": "2026-06-28T00:00:00+00:00",
                "host_name": "host-a",
                "method": "mydumper",
                "dumped_instances": [
                    {
                        "instance_uid": "site-a",
                        "instance_name": "site-a.example.com",
                        "database": "baza_site_a",
                        "bytes": 123,
                        "path": "databases/site-a__baza_site_a",
                    }
                ],
            }
            inst = MauticInstall(
                instance_uid="site-a",
                name="site-a.example.com",
                root="/var/www/site-a/public_html",
                console_path="/var/www/site-a/public_html/bin/console",
                primary_domain="site-a.example.com",
                mautic_major=6,
                db=DBConfig("localhost", 3306, "baza_site_a", "u", "p", ""),
            )

            _write_host_backup_instance_manifests(
                mount_path=mount,
                backup_dir=backup_dir,
                marker=marker,
                instances=[inst],
            )

            sidecar_manifest = (
                mount
                / "backup"
                / "host-a"
                / "instances"
                / "site-a.example.com"
                / "2026-06-28"
                / "mcc-backup-manifest.json"
            )
            self.assertTrue(sidecar_manifest.exists())
            payload = json.loads(sidecar_manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["kind"], "mcc.instance_backup.from_host_mydumper")
            self.assertEqual(payload["source_domain"], "site-a.example.com")
            self.assertEqual(payload["source_database"], "baza_site_a")
            self.assertEqual(payload["parent_backup_dir"], "backup/host-a/2026-06-28")
            self.assertEqual(payload["restore_scope"], "instance")
            self.assertTrue((mount / "mcc-backups-index.d" / "backup-host-a-instances-site-a.example.com-2026-06-28.json").exists())


if __name__ == "__main__":
    unittest.main()
