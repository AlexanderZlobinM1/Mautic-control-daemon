from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent.mautic_upgrade import (
    _clean_target_version,
    _clear_prod_cache_with_fallback,
    _enter_upgrade_maintenance,
    _exit_upgrade_maintenance,
    _hard_clear_prod_cache,
    _upgrade_target_relation,
)


class MauticUpgradeTargetTests(unittest.TestCase):
    def test_patch_upgrade_is_allowed_without_minor_flag(self) -> None:
        self.assertEqual(_upgrade_target_relation("7.1.1", "7.1.2"), "allowed")

    def test_one_step_minor_requires_flag(self) -> None:
        self.assertEqual(_upgrade_target_relation("7.0.2", "7.1.2"), "blocked_minor")
        self.assertEqual(_upgrade_target_relation("7.0.2", "7.1.2", allow_minor=True), "allowed")
        self.assertEqual(_upgrade_target_relation("5.1.4", "5.2.11", allow_minor=True), "allowed")

    def test_major_and_multi_minor_are_blocked(self) -> None:
        self.assertEqual(_upgrade_target_relation("7.0.2", "8.0.0", allow_minor=True), "blocked_major")
        self.assertEqual(_upgrade_target_relation("7.0.2", "7.2.0", allow_minor=True), "blocked_minor")

    def test_target_must_be_clean_semver(self) -> None:
        self.assertEqual(_clean_target_version("7.1.2"), "7.1.2")
        with self.assertRaises(RuntimeError):
            _clean_target_version("bad 7.1.2")

    def test_upgrade_maintenance_guard_owns_pause_and_cron_restore(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pause_flag = Path(td) / "scheduler.pause"
            cfg = SimpleNamespace(scheduler_pause_flag_path=str(pause_flag))
            with (
                patch(
                    "mcd_agent.mautic_upgrade.stop_cron_service",
                    return_value={"ok": True, "unit": "cron", "was_active": True},
                ) as stop_cron,
                patch(
                    "mcd_agent.mautic_upgrade.restore_cron_service_if_needed",
                    return_value={"ok": True, "unit": "cron", "started": True},
                ) as restore_cron,
            ):
                guard = _enter_upgrade_maintenance(cfg)
                self.assertTrue(pause_flag.exists())
                self.assertTrue(guard.owned_pause_flag)
                self.assertTrue(guard.owned_cron_stop)
                stop_cron.assert_called_once_with(cfg)

                _exit_upgrade_maintenance(cfg, guard)
                self.assertFalse(pause_flag.exists())
                restore_cron.assert_called_once_with(cfg)

    def test_upgrade_maintenance_guard_preserves_existing_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pause_flag = Path(td) / "scheduler.pause"
            marker = Path(td) / "maintenance.cron.stopped.json"
            pause_flag.write_text("paused\n", encoding="utf-8")
            marker.write_text('{"unit":"cron","was_active":true}', encoding="utf-8")
            cfg = SimpleNamespace(scheduler_pause_flag_path=str(pause_flag))
            with (
                patch("mcd_agent.mautic_upgrade.stop_cron_service") as stop_cron,
                patch("mcd_agent.mautic_upgrade.restore_cron_service_if_needed") as restore_cron,
            ):
                guard = _enter_upgrade_maintenance(cfg)
                self.assertFalse(guard.owned_pause_flag)
                self.assertFalse(guard.owned_cron_stop)
                stop_cron.assert_not_called()

                _exit_upgrade_maintenance(cfg, guard)
                self.assertTrue(pause_flag.exists())
                restore_cron.assert_not_called()

    def test_hard_clear_prod_cache_recreates_prod(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prod = root / "var" / "cache" / "prod"
            prod.mkdir(parents=True)
            (prod / "stale.php").write_text("old\n", encoding="utf-8")

            _hard_clear_prod_cache(str(root))

            self.assertTrue(prod.is_dir())
            self.assertFalse((prod / "stale.php").exists())

    def test_hard_clear_prod_cache_continues_when_old_tree_delete_races(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prod = root / "var" / "cache" / "prod"
            prod.mkdir(parents=True)
            (prod / "stale.php").write_text("old\n", encoding="utf-8")

            with (
                patch("mcd_agent.mautic_upgrade.shutil.rmtree", side_effect=OSError("Directory not empty")),
                patch("mcd_agent.mautic_upgrade.subprocess.run") as run,
            ):
                run.return_value.returncode = 1
                run.return_value.stdout = ""
                run.return_value.stderr = "Directory not empty"
                _hard_clear_prod_cache(str(root))

            self.assertTrue(prod.is_dir())
            self.assertFalse((prod / "stale.php").exists())

    def test_standard_cache_clear_does_not_hard_clear_when_successful(self) -> None:
        with (
            patch("mcd_agent.mautic_upgrade._run_capture") as run_capture,
            patch("mcd_agent.mautic_upgrade._hard_clear_prod_cache") as hard_clear,
        ):
            run_capture.return_value.returncode = 0
            run_capture.return_value.stdout = ""
            run_capture.return_value.stderr = ""

            _clear_prod_cache_with_fallback("/tmp/app", "bin/console", "php")

            hard_clear.assert_not_called()

    def test_standard_cache_clear_falls_back_to_hard_clear_on_failure(self) -> None:
        with (
            patch("mcd_agent.mautic_upgrade._run_capture") as run_capture,
            patch("mcd_agent.mautic_upgrade._hard_clear_prod_cache") as hard_clear,
            patch("mcd_agent.mautic_upgrade._run") as run,
        ):
            run_capture.return_value.returncode = 1
            run_capture.return_value.stdout = "cache error"
            run_capture.return_value.stderr = ""

            _clear_prod_cache_with_fallback("/tmp/app", "bin/console", "php")

            hard_clear.assert_called_once_with("/tmp/app")
            run.assert_called_once_with(["php", "bin/console", "cache:clear"], cwd="/tmp/app", as_www_data=True)


if __name__ == "__main__":
    unittest.main()
