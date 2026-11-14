from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent.mautic_upgrade import _apply_composer


class ComposerUpgradeScriptTests(unittest.TestCase):
    def run_upgrade(self, *, scripts=None, version="Composer version 2.9.5", fail=None):
        self.events = []
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            root = Path(td)
            manifest = root / "composer.json"
            manifest.write_text(json.dumps({"require": {"mautic/core-lib": "7.1.3"}, "scripts": scripts or {}}))
            stale = root / "var/cache/prod/old-container.php"
            stale.parent.mkdir(parents=True)
            stale.write_text("old services")
            stack.enter_context(patch("mcd_agent.mautic_upgrade._resolve_composer_project_root", return_value=td))
            stack.enter_context(patch("mcd_agent.mautic_upgrade._command_version_line", return_value=version))
            for name in ("_apply_mautic7_twig_include_hotfix", "_normalize_mautic7_loopback_redis_cache"):
                stack.enter_context(patch("mcd_agent.mautic_upgrade." + name, return_value=False))
            stack.enter_context(patch("mcd_agent.composer_permissions.prepare_composer_paths", return_value={}))

            def record(name):
                def called(*args, **kwargs):
                    self.events.append(name)
                    if fail == name:
                        raise RuntimeError(name)
                    if name == "purge":
                        stale.unlink()
                return called

            for name, label in (("_hard_clear_prod_cache", "purge"),
                                ("_clear_prod_cache_with_fallback", "cache-clear"),
                                ("_run_doctrine_migrate_with_reconcile", "migrate"),
                                ("_verify_or_reconcile_doctrine_migrations", "verify")):
                stack.enter_context(patch("mcd_agent.mautic_upgrade." + name, side_effect=record(label)))
            stack.enter_context(patch("mcd_agent.mautic_upgrade._doctrine_migrate_command", return_value="doctrine:migrations:migrate"))

            def run(cmd, **kwargs):
                self.assertEqual(kwargs, {"cwd": td, "as_www_data": True})
                if "--dry-run" in cmd:
                    self.assertIn("--no-scripts", cmd)
                    record("dry-run")()
                elif "update" in cmd:
                    self.assertEqual(cmd[:3], ["env", "COMPOSER_SKIP_SCRIPTS=post-autoload-dump,post-update-cmd", "composer"])
                    self.assertNotIn("--no-plugins", cmd)
                    self.assertNotIn("--no-scripts", cmd)
                    self.assertTrue(stale.exists())
                    record("update")()
                elif "run-script" in cmd:
                    self.assertFalse(stale.exists(), "scripts must not boot the previous core's container")
                    record(cmd[-1])()
                else:
                    self.assertIn("--finish", cmd)
                    record("finish")()

            stack.enter_context(patch("mcd_agent.mautic_upgrade._run", side_effect=run))
            _apply_composer(
                td,
                "bin/console",
                "php",
                "7.1.3",
                "7.2.0",
                composer_bin="composer",
            )

    def test_update_purges_stale_container_before_replaying_hooks_and_migrations(self):
        self.run_upgrade(scripts={"post-autoload-dump": "callback", "post-update-cmd": ["@generate-assets"]})
        self.assertEqual(self.events, ["dry-run", "update", "purge", "post-autoload-dump", "post-update-cmd",
                                       "cache-clear", "finish", "migrate", "verify"])

    def test_missing_hooks_are_not_invented(self):
        self.run_upgrade()
        self.assertEqual(self.events, ["dry-run", "update", "purge", "cache-clear", "finish", "migrate", "verify"])

    def test_dry_run_failure_never_changes_packages_or_clears_cache(self):
        with self.assertRaisesRegex(RuntimeError, "dry-run"):
            self.run_upgrade(fail="dry-run")
        self.assertEqual(self.events, ["dry-run"])

    def test_update_failure_does_not_run_application_hooks_or_migrations(self):
        with self.assertRaisesRegex(RuntimeError, "update"):
            self.run_upgrade(fail="update")
        self.assertEqual(self.events, ["dry-run", "update"])

    def test_cache_purge_failure_stops_before_hooks(self):
        with self.assertRaisesRegex(RuntimeError, "purge"):
            self.run_upgrade(fail="purge")
        self.assertEqual(self.events, ["dry-run", "update", "purge"])

    def test_hook_failure_is_not_mistaken_for_completed_upgrade(self):
        with self.assertRaisesRegex(RuntimeError, "post-update-cmd"):
            self.run_upgrade(scripts={"post-update-cmd": "assets"}, fail="post-update-cmd")
        self.assertEqual(self.events, ["dry-run", "update", "purge", "post-update-cmd"])

    def test_unsupported_or_unknown_composer_stops_before_update(self):
        for version in ("Composer version 2.8.5", "Composer version unknown"):
            with self.subTest(version=version), self.assertRaisesRegex(RuntimeError, "Composer >= 2.8.6"):
                self.run_upgrade(version=version)
            self.assertEqual(self.events, [])
