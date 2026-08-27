from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcd_agent import mautic_locks


class MauticFileLocksTests(unittest.TestCase):
    def _run_dir(self, root: Path) -> Path:
        run_dir = root / "var" / "cache" / "run"
        run_dir.mkdir(parents=True)
        return run_dir

    def test_dead_pid_lock_is_cleared_even_when_recent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = self._run_dir(root) / "sf.mautic-segments-update61.abc.lock"
            path.write_text("12345\n", encoding="utf-8")

            with patch.object(mautic_locks, "_pid_alive", return_value=False):
                payload = mautic_locks.cleanup_stale_mautic_file_locks(root, min_age_sec=21600)

            self.assertEqual(payload["cleared_file_locks"], 1)
            self.assertFalse(path.exists())
            self.assertEqual(payload["file_locks"][0]["reason"], "dead_pid")

    def test_live_pid_lock_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = self._run_dir(root) / "sf.mautic-segments-update57.abc.lock"
            path.write_text("12345\n", encoding="utf-8")

            with patch.object(mautic_locks, "_pid_alive", return_value=True):
                payload = mautic_locks.cleanup_stale_mautic_file_locks(root, min_age_sec=0)

            self.assertEqual(payload["cleared_file_locks"], 0)
            self.assertTrue(path.exists())
            self.assertEqual(payload["file_locks"][0]["reason"], "pid_alive")

    def test_unknown_pid_lock_requires_age_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir = self._run_dir(root)
            old_lock = run_dir / "sf.mautic-messages-send.old.lock"
            young_lock = run_dir / "sf.mautic-messages-send.young.lock"
            old_lock.write_text("not-a-pid\n", encoding="utf-8")
            young_lock.write_text("not-a-pid\n", encoding="utf-8")
            now = 1_800_000_000.0
            os.utime(old_lock, (now - 7200, now - 7200))
            os.utime(young_lock, (now - 60, now - 60))

            payload = mautic_locks.cleanup_stale_mautic_file_locks(root, min_age_sec=3600, now_ts=now)

            self.assertEqual(payload["cleared_file_locks"], 1)
            self.assertFalse(old_lock.exists())
            self.assertTrue(young_lock.exists())
            reasons = {row["path"]: row["reason"] for row in payload["file_locks"]}
            self.assertEqual(reasons[str(old_lock)], "unknown_pid_old")
            self.assertEqual(reasons[str(young_lock)], "unknown_pid_young")


if __name__ == "__main__":
    unittest.main()
