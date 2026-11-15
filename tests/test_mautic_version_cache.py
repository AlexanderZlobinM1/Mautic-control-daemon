from __future__ import annotations

from pathlib import Path
import json
from contextlib import redirect_stdout
from io import StringIO
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcd_agent import mautic_version_cache

try:
    from mcd_agent import mautic_upgrade
except ModuleNotFoundError:
    mautic_upgrade = None


class MauticVersionCacheTest(unittest.TestCase):
    @unittest.skipIf(mautic_upgrade is None, "upgrade check dependencies are not installed")
    def test_upgrade_check_marks_static_evidence_authoritative(self) -> None:
        install = SimpleNamespace(root="/var/www/site", console_path="/var/www/site/bin/console", runtime="host")
        with (
            patch.object(mautic_upgrade, "_pick_install_record", return_value=install),
            patch.object(mautic_upgrade, "_latest_same_branch", return_value="7.2.0"),
            patch.object(
                mautic_upgrade,
                "read_mautic_version_evidence_read_only",
                return_value={"version": "7.1.3", "source": "static_metadata"},
            ),
        ):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(mautic_upgrade.run_upgrade_check(SimpleNamespace(), None), 0)
            marker = next(line for line in output.getvalue().splitlines() if line.startswith("MCD_UPGRADE_VERSION_EVIDENCE="))
            evidence = json.loads(marker.split("=", 1)[1])
            self.assertEqual(evidence["version_source"], "static_metadata")
            self.assertTrue(evidence["authoritative"])

    @unittest.skipIf(mautic_upgrade is None, "upgrade check dependencies are not installed")
    def test_upgrade_check_marks_cache_fallback_non_authoritative(self) -> None:
        install = SimpleNamespace(root="/var/www/site", console_path="/var/www/site/bin/console", runtime="host")
        with (
            patch.object(mautic_upgrade, "_pick_install_record", return_value=install),
            patch.object(mautic_upgrade, "_latest_same_branch", return_value=None),
            patch.object(
                mautic_upgrade,
                "read_mautic_version_evidence_read_only",
                return_value={"version": "7.2.0", "source": "cache_fallback"},
            ),
        ):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(mautic_upgrade.run_upgrade_check(SimpleNamespace(), None), 1)
            marker = next(line for line in output.getvalue().splitlines() if line.startswith("MCD_UPGRADE_VERSION_EVIDENCE="))
            evidence = json.loads(marker.split("=", 1)[1])
            self.assertEqual(evidence["version_source"], "cache_fallback")
            self.assertIsNone(evidence["current_version"])
            self.assertFalse(evidence["authoritative"])
            self.assertIn("current=unknown", output.getvalue())
            self.assertNotIn("current=7.2.0", output.getvalue())
            self.assertIn("next=none", output.getvalue())

    @unittest.skipIf(mautic_upgrade is None, "upgrade check dependencies are not installed")
    def test_upgrade_check_uses_rollback_static_metadata_and_install_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "site"
            install_root = project / "public_html"
            install_root.mkdir(parents=True)
            (project / "composer.lock").write_text(
                '{"packages":[{"name":"mautic/core-lib","version":"7.1.3"}]}',
                encoding="utf-8",
            )
            generated = Path(td) / "generated"
            install = SimpleNamespace(
                root=str(install_root),
                console_path=str(install_root / "bin/console"),
                runtime="host",
            )
            with (
                patch.object(mautic_version_cache, "_VERSION_CACHE_ROOT", generated),
                patch.object(mautic_upgrade, "_pick_install_record", return_value=install),
                patch.object(mautic_upgrade, "_latest_same_branch", return_value="7.2.0"),
            ):
                mautic_version_cache.write_mautic_version_cache(install_root, "7.2.0")
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(mautic_upgrade.run_upgrade_check(SimpleNamespace(), None), 0)
            text = output.getvalue()
            marker = next(line for line in text.splitlines() if line.startswith("MCD_UPGRADE_VERSION_EVIDENCE="))
            evidence = json.loads(marker.split("=", 1)[1])
            self.assertEqual(evidence["root"], str(install_root))
            self.assertEqual(evidence["current_version"], "7.1.3")
            self.assertEqual(evidence["version_source"], "static_metadata")
            self.assertIn(f"root={install_root}", text)
            self.assertIn("current=7.1.3", text)

    @unittest.skipIf(mautic_upgrade is None, "upgrade dependencies are not installed")
    def test_composer_prepare_does_not_bootstrap_for_cache_fallback(self) -> None:
        install = SimpleNamespace(root="/var/www/site", console_path="/var/www/site/bin/console", runtime="host", instance_uid="site")
        config = SimpleNamespace(php_bin="php")
        with (
            patch.object(mautic_upgrade, "_pick_install_record", return_value=install),
            patch.object(mautic_upgrade, "read_mautic_version_evidence_read_only", return_value={"version": "7.2.0", "source": "cache_fallback"}),
            patch.object(mautic_upgrade, "composer_readiness", return_value={"status": "missing", "php": {}}) as readiness,
        ):
            self.assertEqual(
                mautic_upgrade.run_upgrade_composer_prepare(
                    config=config, root=None, mode="composer", target_override="7.2.0"
                ),
                1,
            )
            self.assertEqual(readiness.call_count, 1)
            self.assertFalse(readiness.call_args.kwargs["allow_bootstrap"])

    @unittest.skipIf(mautic_upgrade is None, "upgrade dependencies are not installed")
    def test_repair_authorization_rejects_cache_fallback(self) -> None:
        install = SimpleNamespace(root="/var/www/site", console_path="/var/www/site/bin/console", runtime="host", instance_uid="site")
        with (
            patch.object(mautic_upgrade, "_pick_install_record", return_value=install),
            patch.object(mautic_upgrade, "read_mautic_version_evidence_read_only", return_value={"version": "6.0.9", "source": "cache_fallback"}),
            patch.object(mautic_upgrade, "issue_repair_authorization_context") as authorize,
        ):
            with self.assertRaisesRegex(RuntimeError, "authoritative on-disk"):
                mautic_upgrade.run_upgrade_authorize_repair(
                    config=SimpleNamespace(), root=None, target_override="7.1.3", repair_plan_json="{}", backup_manifest_path="/tmp/backup"
                )
            authorize.assert_not_called()

    def test_candidate_roots_do_not_escape_to_shared_var_www(self) -> None:
        self.assertEqual(
            mautic_version_cache._candidate_roots("/var/www/client/public_html"),
            [Path("/var/www/client/public_html"), Path("/var/www/client")],
        )

    def test_cached_version_with_wrong_major_is_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site" / "public_html"
            root.mkdir(parents=True)
            console = root / "bin" / "console"
            console.parent.mkdir()
            console.write_text("<?php\n", encoding="utf-8")
            generated = Path(td) / "generated"

            with (
                patch.object(mautic_version_cache, "_VERSION_CACHE_ROOT", generated),
                patch.object(mautic_version_cache, "_read_version_from_mcd_source", return_value="6.0.9"),
            ):
                mautic_version_cache.write_mautic_version_cache(root, "7.1.3")
                actual = mautic_version_cache.collect_mautic_version(
                    str(root),
                    "/usr/bin/php",
                    console_path=str(console),
                    expected_major=6,
                )

                self.assertEqual(actual, "6.0.9")
                self.assertEqual(mautic_version_cache.read_cached_mautic_version(root), "6.0.9")

    def test_newer_same_major_metadata_refreshes_stale_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site" / "public_html"
            root.mkdir(parents=True)
            (root / "composer.lock").write_text(
                '{"packages":[{"name":"mautic/core-lib","version":"7.2.0"}]}',
                encoding="utf-8",
            )
            generated = Path(td) / "generated"

            with (
                patch.object(mautic_version_cache, "_VERSION_CACHE_ROOT", generated),
                patch.object(mautic_version_cache, "_read_version_from_mcd_source", return_value="7.2.0") as runtime,
            ):
                mautic_version_cache.write_mautic_version_cache(root, "7.1.3")
                actual = mautic_version_cache.collect_mautic_version(
                    str(root), "/usr/bin/php", expected_major=7
                )

                self.assertEqual(actual, "7.2.0")
                self.assertEqual(mautic_version_cache.read_cached_mautic_version(root), "7.2.0")
                runtime.assert_not_called()

    def test_read_only_version_uses_static_metadata_without_console(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            root.mkdir(parents=True)
            (root / "composer.lock").write_text(
                '{"packages":[{"name":"mautic/core-lib","version":"7.2.0"}]}',
                encoding="utf-8",
            )
            with patch.object(mautic_version_cache, "_VERSION_CACHE_ROOT", root / "generated"), patch(
                "mcd_agent.mautic_version_cache.subprocess.run",
                side_effect=AssertionError("read-only version probe invoked subprocess"),
            ):
                self.assertEqual(mautic_version_cache.read_mautic_version_read_only(root), "7.2.0")

    def test_read_only_version_rejects_stale_ahead_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            root.mkdir(parents=True)
            (root / "composer.lock").write_text(
                '{"packages":[{"name":"mautic/core-lib","version":"7.1.3"}]}',
                encoding="utf-8",
            )
            with patch.object(mautic_version_cache, "_VERSION_CACHE_ROOT", root / "generated"):
                mautic_version_cache.write_mautic_version_cache(root, "7.2.0")
                self.assertEqual(mautic_version_cache.read_mautic_version_read_only(root), "7.1.3")

    def test_read_only_evidence_marks_cache_fallback_non_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            root.mkdir(parents=True)
            with patch.object(mautic_version_cache, "_VERSION_CACHE_ROOT", root / "generated"):
                mautic_version_cache.write_mautic_version_cache(root, "7.2.0")
                self.assertEqual(
                    mautic_version_cache.read_mautic_version_evidence_read_only(root),
                    {"version": "7.2.0", "source": "cache_fallback"},
                )

    def test_read_only_evidence_fails_closed_on_conflicting_static_sources(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            (root / "docroot/app").mkdir(parents=True)
            (root / "docroot/app/release_metadata.json").write_text('{"version":"7.1.3"}', encoding="utf-8")
            (root / "composer.lock").write_text(
                '{"packages":[{"name":"mautic/core-lib","version":"7.2.0"}]}',
                encoding="utf-8",
            )
            self.assertEqual(
                mautic_version_cache.read_mautic_version_evidence_read_only(root),
                {"version": None, "source": "conflicting_static_metadata"},
            )

    def test_exact_release_identity_accepts_same_major_non_exact_composer_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            (root / "app").mkdir(parents=True)
            (root / "vendor/composer").mkdir(parents=True)
            (root / "app/release_metadata.json").write_text('{"version":"6.0.9"}', encoding="utf-8")
            package = {"name": "mautic/core-lib", "version": "6.0.0-dev"}
            (root / "composer.lock").write_text(json.dumps({"packages": [package]}), encoding="utf-8")
            (root / "vendor/composer/installed.json").write_text(
                json.dumps({"packages": [package]}), encoding="utf-8"
            )

            with patch("mcd_agent.mautic_version_cache.subprocess.run", side_effect=AssertionError("external command")):
                self.assertEqual(
                    mautic_version_cache.read_mautic_version_evidence_read_only(root),
                    {"version": "6.0.9", "source": "static_metadata"},
                )

    def test_exact_release_identity_rejects_competing_installed_version(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            (root / "app").mkdir(parents=True)
            (root / "vendor/composer").mkdir(parents=True)
            (root / "app/release_metadata.json").write_text('{"version":"6.0.9"}', encoding="utf-8")
            (root / "composer.lock").write_text(
                json.dumps({"packages": [{"name": "mautic/core-lib", "version": "6.0.0-dev"}]}),
                encoding="utf-8",
            )
            (root / "vendor/composer/installed.json").write_text(
                json.dumps({"packages": [{"name": "mautic/core-lib", "version": "6.0.8"}]}),
                encoding="utf-8",
            )

            self.assertEqual(
                mautic_version_cache.read_mautic_version_evidence_read_only(root),
                {"version": None, "source": "conflicting_static_metadata"},
            )

    def test_inventory_refreshes_cache_downward_from_static_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            root.mkdir(parents=True)
            (root / "composer.lock").write_text(
                '{"packages":[{"name":"mautic/core-lib","version":"7.1.3"}]}',
                encoding="utf-8",
            )
            with patch.object(mautic_version_cache, "_VERSION_CACHE_ROOT", root / "generated"):
                mautic_version_cache.write_mautic_version_cache(root, "7.2.0")
                self.assertEqual(mautic_version_cache.collect_mautic_version(str(root), "/usr/bin/php"), "7.1.3")
                self.assertEqual(mautic_version_cache.read_cached_mautic_version(root), "7.1.3")

    def test_older_package_metadata_does_not_downgrade_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            root.mkdir()
            (root / "composer.lock").write_text(
                '{"packages":[{"name":"mautic/core-lib","version":"7.1.3"}]}',
                encoding="utf-8",
            )
            generated = Path(td) / "generated"

            with (
                patch.object(mautic_version_cache, "_VERSION_CACHE_ROOT", generated),
                patch.object(mautic_version_cache, "_read_version_from_mcd_source") as runtime,
            ):
                mautic_version_cache.write_mautic_version_cache(root, "7.2.0")
                self.assertEqual(
                    mautic_version_cache.collect_mautic_version(str(root), "/usr/bin/php"),
                    "7.1.3",
                )
                runtime.assert_not_called()

    def test_migrates_legacy_cache_outside_instance_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "var" / "www" / "site" / "public_html"
            legacy = root / ".mcd" / "mautic.version"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("6.0.9\n", encoding="utf-8")
            generated = Path(td) / "opt" / "mcd" / "generated" / "mautic-version"

            with patch.object(mautic_version_cache, "_VERSION_CACHE_ROOT", generated):
                self.assertTrue(mautic_version_cache.migrate_legacy_mautic_version_cache(root))
                self.assertEqual(mautic_version_cache.read_cached_mautic_version(root), "6.0.9")
                self.assertTrue(mautic_version_cache.version_cache_path(root).is_file())

            self.assertEqual(legacy.read_text(encoding="utf-8"), "6.0.9\n")

    def test_confirmed_major_requires_runtime_and_lock_agreement(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            console = root / "bin" / "console"
            console.parent.mkdir(parents=True)
            console.write_text("<?php\n", encoding="utf-8")

            with (
                patch.object(mautic_version_cache, "descriptor_for_root", return_value=None),
                patch.object(mautic_version_cache, "_read_version_from_runtime_only", return_value="7.1.3"),
                patch.object(mautic_version_cache, "_read_major_from_composer_lock", return_value=7),
            ):
                major = mautic_version_cache.confirmed_mautic_major(
                    str(root),
                    "/usr/bin/php",
                    console_path=str(console),
                    expected_major=7,
                )

            self.assertEqual(major, 7)

    def test_confirmed_major_returns_unknown_for_missing_or_conflicting_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            console = root / "bin" / "console"
            console.parent.mkdir(parents=True)
            console.write_text("<?php\n", encoding="utf-8")
            cases = (
                ("6.0.9", 7, 6),
                (None, 7, 7),
                ("7.1.3", 7, 6),
            )

            for runtime, lock, discovered in cases:
                with self.subTest(runtime=runtime, lock=lock, discovered=discovered), patch.object(
                    mautic_version_cache,
                    "descriptor_for_root",
                    return_value=None,
                ), patch.object(
                    mautic_version_cache,
                    "_read_version_from_runtime_only",
                    return_value=runtime,
                ), patch.object(
                    mautic_version_cache,
                    "_read_major_from_composer_lock",
                    return_value=lock,
                ):
                    self.assertIsNone(
                        mautic_version_cache.confirmed_mautic_major(
                            str(root),
                            "/usr/bin/php",
                            console_path=str(console),
                            expected_major=discovered,
                        )
                    )

    def test_confirmed_major_accepts_zip_install_without_composer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            console = root / "bin" / "console"
            console.parent.mkdir(parents=True)
            console.write_text("<?php\n", encoding="utf-8")

            with (
                patch.object(mautic_version_cache, "descriptor_for_root", return_value=None),
                patch.object(mautic_version_cache, "_read_version_from_runtime_only", return_value="4.4.13"),
                patch.object(mautic_version_cache, "_read_major_from_composer_lock", return_value=None),
            ):
                major = mautic_version_cache.confirmed_mautic_major(
                    str(root),
                    "/usr/bin/php",
                    console_path=str(console),
                    local_php_path=str(root / "app" / "config" / "local.php"),
                    expected_major=4,
                )

            self.assertEqual(major, 4)

    def test_confirmed_major_rejects_mautic4_layout_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            console = root / "bin" / "console"
            console.parent.mkdir(parents=True)
            console.write_text("<?php\n", encoding="utf-8")

            with (
                patch.object(mautic_version_cache, "descriptor_for_root", return_value=None),
                patch.object(mautic_version_cache, "_read_version_from_runtime_only", return_value="7.1.3"),
                patch.object(mautic_version_cache, "_read_major_from_composer_lock", return_value=7),
            ):
                major = mautic_version_cache.confirmed_mautic_major(
                    str(root),
                    "/usr/bin/php",
                    console_path=str(console),
                    local_php_path=str(root / "app" / "config" / "local.php"),
                    expected_major=7,
                )

            self.assertIsNone(major)

    def test_composer_major_supports_x_dev_release_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "composer.lock").write_text(
                '{"packages":[{"name":"mautic/core-lib","version":"4.3.x-dev"}]}',
                encoding="utf-8",
            )

            self.assertEqual(mautic_version_cache._read_major_from_composer_lock(root), 4)


if __name__ == "__main__":
    unittest.main()
