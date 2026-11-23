from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mcd_agent.instance_size import INSTANCE_SIZE_REFRESH_SEC, InstanceSizeCache, collect_instance_sizes
from mcd_agent.models import DBConfig, MauticInstall


class InstanceSizeTests(unittest.TestCase):
    class RuntimeStore:
        def __init__(self, path: str) -> None:
            self.path = path
            self.payload = None
            self.writes: list[dict[str, object]] = []

        def get_runtime_sync(self, _key: str):
            return self.payload

        def put_runtime_sync(self, _key: str, payload: dict[str, object]) -> None:
            self.payload = deepcopy(payload)
            self.writes.append(deepcopy(payload))

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

    def test_persists_each_instance_and_reuses_values_for_24_hours(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root_a = Path(td) / "a"
            root_b = Path(td) / "b"
            (root_a / "media").mkdir(parents=True)
            root_b.mkdir()
            installs = [
                MauticInstall("uid-b", "b.test", str(root_b), str(root_b / "bin" / "console")),
                MauticInstall("uid-a", "a.test", str(root_a), str(root_a / "bin" / "console")),
            ]
            store = self.RuntimeStore(str(Path(td) / "mcd-state.db"))
            du_calls: list[str] = []

            def fake_du(path: Path, **_kwargs):
                du_calls.append(str(path))
                return 4096, None

            with patch("mcd_agent.instance_size._du_bytes", side_effect=fake_du):
                first = InstanceSizeCache(store).collect(installs, now_ts=1000)
                calls_after_first = len(du_calls)
                second = InstanceSizeCache(store).collect(
                    installs,
                    now_ts=1000 + INSTANCE_SIZE_REFRESH_SEC - 1,
                )

            self.assertEqual(len(store.writes), 2)
            self.assertEqual(list(store.writes[0]["instances"]), ["uid-a"])
            self.assertEqual(list(store.writes[1]["instances"]), ["uid-a", "uid-b"])
            self.assertEqual(len(du_calls), calls_after_first)
            self.assertEqual(first, second)

            with patch("mcd_agent.instance_size._du_bytes", return_value=(8192, None)) as du:
                InstanceSizeCache(store).collect(installs, now_ts=1000 + INSTANCE_SIZE_REFRESH_SEC + 1)
            self.assertGreater(du.call_count, 0)

    def test_du_scans_are_globally_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            roots = [Path(td) / "a", Path(td) / "b"]
            for root in roots:
                root.mkdir()
            installs = [
                MauticInstall(f"uid-{index}", f"{index}.test", str(root), str(root / "bin" / "console"))
                for index, root in enumerate(roots)
            ]
            active = 0
            max_active = 0
            guard = threading.Lock()

            def fake_du(_path: Path, **_kwargs):
                nonlocal active, max_active
                with guard:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.01)
                with guard:
                    active -= 1
                return 1024, None

            with patch("mcd_agent.instance_size._du_bytes", side_effect=fake_du):
                threads = [
                    threading.Thread(target=InstanceSizeCache().collect, args=([install],))
                    for install in installs
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(max_active, 1)
