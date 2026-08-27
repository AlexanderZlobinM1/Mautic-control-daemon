from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent.instance_size import collect_instance_sizes
from mcd_agent.models import DBConfig, MauticInstall


class InstanceSizeTests(unittest.TestCase):
    def test_collects_root_db_total_and_breakdown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "mautic"
            (root / "media").mkdir(parents=True)
            (root / "media" / "asset.bin").write_bytes(b"x" * 128)
            inst = MauticInstall(
                instance_uid="uid-1",
                name="example.test",
                root=str(root),
                console_path=str(root / "bin" / "console"),
                db=DBConfig(host="localhost", port=3306, name="mautic", user="u", password="p", table_prefix=""),
            )

            with patch("mcd_agent.instance_size.MauticDB") as db_cls:
                db_cls.return_value.fetch_rows.return_value = [{"size_bytes": 2048}]
                rows = collect_instance_sizes([inst])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["instance_uid"], "uid-1")
        self.assertEqual(rows[0]["db_bytes"], 2048)
        self.assertGreaterEqual(rows[0]["root_bytes"] or 0, 128)
        self.assertEqual(rows[0]["total_bytes"], (rows[0]["root_bytes"] or 0) + 2048)
        self.assertGreaterEqual(rows[0]["breakdown"].get("media", 0), 128)

    def test_missing_root_reports_warning_without_crashing(self) -> None:
        inst = MauticInstall(
            instance_uid="uid-2",
            name="missing.test",
            root="/definitely/missing/mcd-instance-size-test",
            console_path="/definitely/missing/bin/console",
        )

        rows = collect_instance_sizes([inst])

        self.assertEqual(rows[0]["total_bytes"], None)
        self.assertIn("root:path_missing", rows[0]["errors"])
