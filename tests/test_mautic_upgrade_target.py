from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent.mautic_upgrade import (
    _apply_mautic7_twig_include_hotfix,
    _best_probe_domain,
    _clean_target_version,
    _clear_prod_cache_with_fallback,
    _composer_update_args,
    _enter_upgrade_maintenance,
    _exit_upgrade_maintenance,
    _hard_clear_prod_cache,
    _normalize_mautic7_composer_constraints,
    _migrate_php_custom_ini,
    _normalize_mautic7_loopback_redis_cache,
    _rewrite_nginx_php_fpm_references,
    _safe_mautic7_loopback_redis_dsn,
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

    def test_guarded_composer_six_to_seven_major_can_be_allowed(self) -> None:
        self.assertEqual(_upgrade_target_relation("6.0.9", "7.1.2"), "blocked_major")
        self.assertEqual(_upgrade_target_relation("6.0.9", "7.1.2", allow_major=True), "allowed")
        self.assertEqual(_upgrade_target_relation("5.2.9", "7.1.2", allow_major=True), "blocked_major")

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
                patch(
                    "mcd_agent.mautic_upgrade.stop_running_tasks_for_maintenance",
                    return_value={
                        "ok": True,
                        "stopped": 2,
                        "stop_failed": 0,
                        "managed_running": 0,
                        "mautic_console_total": 0,
                    },
                ) as stop_tasks,
            ):
                guard = _enter_upgrade_maintenance(cfg)
                self.assertTrue(pause_flag.exists())
                self.assertTrue(guard.owned_pause_flag)
                self.assertTrue(guard.owned_cron_stop)
                self.assertEqual(guard.stopped_tasks, 2)
                stop_cron.assert_called_once_with(cfg)
                stop_tasks.assert_called_once_with(cfg, grace_sec=30, kill_orphans=True)

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
                patch(
                    "mcd_agent.mautic_upgrade.stop_running_tasks_for_maintenance",
                    return_value={
                        "ok": True,
                        "stopped": 0,
                        "stop_failed": 0,
                        "managed_running": 0,
                        "mautic_console_total": 0,
                    },
                ) as stop_tasks,
            ):
                guard = _enter_upgrade_maintenance(cfg)
                self.assertFalse(guard.owned_pause_flag)
                self.assertFalse(guard.owned_cron_stop)
                stop_cron.assert_not_called()
                stop_tasks.assert_called_once_with(cfg, grace_sec=30, kill_orphans=True)

                _exit_upgrade_maintenance(cfg, guard)
                self.assertTrue(pause_flag.exists())
                restore_cron.assert_not_called()

    def test_upgrade_maintenance_guard_aborts_when_tasks_do_not_stop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pause_flag = Path(td) / "scheduler.pause"
            cfg = SimpleNamespace(scheduler_pause_flag_path=str(pause_flag))
            with (
                patch(
                    "mcd_agent.mautic_upgrade.stop_cron_service",
                    return_value={"ok": True, "unit": "cron", "was_active": True},
                ),
                patch(
                    "mcd_agent.mautic_upgrade.restore_cron_service_if_needed",
                    return_value={"ok": True, "unit": "cron", "started": True},
                ) as restore_cron,
                patch(
                    "mcd_agent.mautic_upgrade.stop_running_tasks_for_maintenance",
                    return_value={
                        "ok": False,
                        "stopped": 0,
                        "stop_failed": 1,
                        "managed_running": 1,
                        "mautic_console_total": 1,
                    },
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "unable to stop all running Mautic tasks"):
                    _enter_upgrade_maintenance(cfg)
                self.assertFalse(pause_flag.exists())
                restore_cron.assert_called_once_with(cfg)

    def test_upgrade_maintenance_guard_allows_clean_race_after_stop_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pause_flag = Path(td) / "scheduler.pause"
            cfg = SimpleNamespace(scheduler_pause_flag_path=str(pause_flag))
            with (
                patch(
                    "mcd_agent.mautic_upgrade.stop_cron_service",
                    return_value={"ok": True, "unit": "cron", "was_active": True},
                ),
                patch(
                    "mcd_agent.mautic_upgrade.stop_running_tasks_for_maintenance",
                    return_value={
                        "ok": False,
                        "stopped": 2,
                        "stop_failed": 4,
                        "managed_running": 0,
                        "mautic_console_total": 0,
                    },
                ),
            ):
                guard = _enter_upgrade_maintenance(cfg)
                self.assertEqual(guard.stop_failed, 4)
                self.assertTrue(pause_flag.exists())

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

    def test_mautic7_loopback_redis_dsn_uses_hex_loopback(self) -> None:
        self.assertEqual(
            _safe_mautic7_loopback_redis_dsn("redis://127.0.0.1:6379/0"),
            "redis://0x7f000001:6379/0",
        )
        self.assertEqual(
            _safe_mautic7_loopback_redis_dsn("redis://:secret@localhost:6380/2"),
            "redis://:secret@0x7f000001:6380/2",
        )
        self.assertEqual(
            _safe_mautic7_loopback_redis_dsn("redis://10.0.0.5:6379/0"),
            "redis://10.0.0.5:6379/0",
        )

    def test_mautic7_loopback_redis_cache_normalizes_local_php(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = root / "config" / "local.php"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(
                """<?php
$parameters = array(
    'cache_adapter' => 'mautic.cache.adapter.redis',
    'cache_adapter_redis' => array(
        'dsn' => 'redis://127.0.0.1:6379/0',
        'options' => array(),
    ),
);
""",
                encoding="utf-8",
            )

            self.assertTrue(_normalize_mautic7_loopback_redis_cache(str(root), "7.1.2"))

            text = cfg.read_text(encoding="utf-8")
            self.assertIn("'dsn' => 'redis://0x7f000001:6379/0'", text)
            self.assertTrue(list(cfg.parent.glob("local.php.mcd-pre-m7-redis-dsn-*.bak")))

    def test_mautic7_loopback_redis_cache_ignores_non_mautic7_or_remote(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = root / "config" / "local.php"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(
                """<?php
$parameters = array(
    'cache_adapter' => 'mautic.cache.adapter.redis',
    'cache_adapter_redis' => array(
        'dsn' => 'redis://10.0.0.5:6379/0',
    ),
);
""",
                encoding="utf-8",
            )

            self.assertFalse(_normalize_mautic7_loopback_redis_cache(str(root), "6.0.9"))
            self.assertFalse(_normalize_mautic7_loopback_redis_cache(str(root), "7.1.2"))
            self.assertIn("redis://10.0.0.5:6379/0", cfg.read_text(encoding="utf-8"))

    def test_mautic7_composer_constraints_upgrade_composer_installers(self) -> None:
        text = """{
  "require": {
    "composer/installers": "^1.11",
    "mautic/core-lib": "7.1.2"
  }
}
"""

        updated, changes = _normalize_mautic7_composer_constraints(text, "7.1.2")

        self.assertEqual(changes, 1)
        self.assertIn('"composer/installers": "^2.0"', updated)

    def test_mautic7_composer_constraints_ignore_non_mautic7(self) -> None:
        text = '{"require":{"composer/installers":"^1.11"}}'

        updated, changes = _normalize_mautic7_composer_constraints(text, "6.0.9")

        self.assertEqual(changes, 0)
        self.assertEqual(updated, text)

    def test_composer_update_uses_full_dependency_resolution_and_dry_run(self) -> None:
        self.assertEqual(_composer_update_args(), ["update", "--with-all-dependencies"])
        self.assertEqual(_composer_update_args(dry_run=True), ["update", "--with-all-dependencies", "--dry-run"])

    def test_php_custom_ini_migration_copies_only_custom_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "8.3" / "cli" / "conf.d"
            dst = root / "8.4" / "cli" / "conf.d"
            src.mkdir(parents=True)
            dst.mkdir(parents=True)
            (src / "60-custom.ini").write_text("memory_limit=512M\n", encoding="utf-8")
            (src / "20-curl.ini").write_text("extension=curl.so\n", encoding="utf-8")

            moved = _migrate_php_custom_ini(php_etc_root=root)

            self.assertTrue((dst / "60-custom.ini").exists())
            self.assertTrue((src / "60-custom.ini").exists())
            self.assertTrue((src / "20-curl.ini").exists())
            self.assertIn(str(dst / "60-custom.ini"), moved[0])

    def test_nginx_php_fpm_rewrite_updates_socket_reference(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            site = root / "sites-enabled"
            site.mkdir()
            conf = site / "app.conf"
            conf.write_text("fastcgi_pass unix:/run/php/php8.3-fpm.sock;\n", encoding="utf-8")

            changed = _rewrite_nginx_php_fpm_references(nginx_roots=(site,))

            self.assertEqual(changed, [str(conf)])
            self.assertIn("php8.4-fpm.sock", conf.read_text(encoding="utf-8"))

    def test_mautic7_twig_include_hotfix_casts_markup_return(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            helper = (
                root
                / "docroot"
                / "app"
                / "bundles"
                / "CoreBundle"
                / "Twig"
                / "Extension"
                / "OverrideIncludeExtension.php"
            )
            helper.parent.mkdir(parents=True)
            helper.write_text(
                """<?php
final class OverrideIncludeExtension
{
    public function includeWithEvent(): string
    {
        return CoreExtension::include($env, $context, $template);
    }
}
""",
                encoding="utf-8",
            )

            self.assertTrue(_apply_mautic7_twig_include_hotfix(str(root), "7.1.2"))

            updated = helper.read_text(encoding="utf-8")
            self.assertIn("return (string) CoreExtension::include(", updated)
            self.assertTrue(helper.with_name(helper.name + ".mcd-pre-twig-include-hotfix.bak").exists())

    def test_probe_domain_falls_back_to_mautic_site_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = root / "config" / "local.php"
            cfg.parent.mkdir(parents=True)
            cfg.write_text("<?php return ['site_url' => 'https://sita.sales-snap.com'];\n", encoding="utf-8")
            inst = SimpleNamespace(primary_domain="", domains=[], local_php_path="", root=str(root))

            self.assertEqual(_best_probe_domain(inst), "sita.sales-snap.com")


if __name__ == "__main__":
    unittest.main()
