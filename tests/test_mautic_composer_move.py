from __future__ import annotations

import tempfile
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mcd_agent.mautic_composer_move as mautic_composer_move
from mcd_agent.discovery import _is_retired_instance_root
from mcd_agent.mautic_composer_move import (
    COMPOSER_TARGET_MARKER,
    ComposerMovePlan,
    _archive_existing_composer_target,
    _archive_source_root,
    _copy_mutable_state,
    _ensure_runtime_dirs,
    _image_ref_for_major,
    _mark_source_root_retired,
    _patch_paths_in_local_php,
    _php_version_for_major,
    _active_cron_wrapper_paths,
    _rewrite_composer_move_cron_wrapper,
    _rewrite_composer_move_crontab,
    _short,
    _target_root,
)


class ComposerMoveHelpersTest(unittest.TestCase):
    def test_crontab_rewrites_old_root_and_retires_removed_email_send_command(self) -> None:
        source = Path("/var/www/ss/public_html")
        target = Path("/var/www/k/public_html")
        content = (
            f"*/5 * * * * php {source}/bin/console mautic:segments:update\n"
            f"* * * * * cd {source} && php bin/console mautic:emails:send\n"
            "0 3 * * * /usr/local/bin/unrelated\n"
        )

        updated, rewritten, retired = _rewrite_composer_move_crontab(
            content,
            source_root=source,
            target_root=target,
            mautic_major=6,
        )

        self.assertEqual(rewritten, 2)
        self.assertEqual(retired, 1)
        self.assertNotIn(str(source), updated)
        self.assertIn(f"php {target}/bin/console mautic:segments:update", updated)
        self.assertIn("# MCD_COMPOSER_MOVE:", updated)
        self.assertNotIn(f"\n* * * * * cd {target}", updated)
        self.assertIn("0 3 * * * /usr/local/bin/unrelated", updated)

    def test_crontab_keeps_email_send_active_for_mautic_four(self) -> None:
        source = Path("/var/www/legacy/public_html")
        target = Path("/var/www/new/public_html")
        content = f"* * * * * php {source}/bin/console mautic:emails:send\n"

        updated, rewritten, retired = _rewrite_composer_move_crontab(
            content,
            source_root=source,
            target_root=target,
            mautic_major=4,
        )

        self.assertEqual((rewritten, retired), (1, 0))
        self.assertEqual(updated, f"* * * * * php {target}/bin/console mautic:emails:send\n")

    def test_active_cron_wrapper_is_migrated_and_email_send_is_retired(self) -> None:
        source = Path("/var/www/ss/public_html")
        target = Path("/var/www/k/public_html")
        with tempfile.TemporaryDirectory() as td:
            wrapper = Path(td) / "mautic-cron.sh"
            original = (
                "#!/bin/sh\n"
                f"php {source}/bin/console mautic:segments:update\n"
                f"php {source}/bin/console mautic:emails:send\n"
            )
            wrapper.write_text(original, encoding="utf-8")
            wrapper.chmod(0o750)
            cron = f"* * * * * {wrapper}\n"

            self.assertEqual(_active_cron_wrapper_paths(cron, source), [wrapper])
            snapshot, rewritten, retired = _rewrite_composer_move_cron_wrapper(
                wrapper,
                source_root=source,
                target_root=target,
                mautic_major=7,
            )

            self.assertEqual(snapshot, original.encode())
            self.assertEqual((rewritten, retired), (2, 1))
            self.assertEqual(wrapper.stat().st_mode & 0o777, 0o750)
            updated = wrapper.read_text(encoding="utf-8")
            self.assertNotIn(str(source), updated)
            self.assertIn(f"php {target}/bin/console mautic:segments:update", updated)
            self.assertNotIn(f"\nphp {target}/bin/console mautic:emails:send", updated)

    def test_commented_cron_wrapper_is_not_migrated(self) -> None:
        source = Path("/var/www/ss/public_html")
        with tempfile.TemporaryDirectory() as td:
            wrapper = Path(td) / "mautic-cron.sh"
            wrapper.write_text(f"php {source}/bin/console mautic:segments:update\n", encoding="utf-8")

            self.assertEqual(_active_cron_wrapper_paths(f"# * * * * * {wrapper}\n", source), [])

    def test_major_maps_to_skeleton_and_php(self) -> None:
        self.assertEqual(_image_ref_for_major(6), "composer6-skeleton")
        self.assertEqual(_image_ref_for_major(7), "composer7-skeleton")
        self.assertEqual(_php_version_for_major(6), "8.3")
        self.assertEqual(_php_version_for_major(7), "8.4")

    def test_short_name_uses_first_domain_label(self) -> None:
        self.assertEqual(_short("Example-Shop.sales-snap.com"), "example_shop")

    def test_new_composer_target_uses_full_fqdn_without_short_name_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "www"
            with patch.object(mautic_composer_move, "COMPOSER_ROOT", base):
                self.assertEqual(
                    _target_root("Example-Shop.sales-snap.com"),
                    base / "example-shop.sales-snap.com" / "public_html",
                )

    def test_only_marked_composer_residue_is_archived(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "example.com" / "public_html"
            target.mkdir(parents=True)
            (target / COMPOSER_TARGET_MARKER).write_text(
                '{"schema":"mcd-composer-target-ownership-v1","state":"in_progress"}\n',
                encoding="utf-8",
            )

            archived = _archive_existing_composer_target(target)

            self.assertIsNotNone(archived)
            assert archived is not None
            self.assertFalse(target.exists())
            self.assertTrue(archived.name.endswith("-public_html"))
            self.assertTrue((archived / COMPOSER_TARGET_MARKER).exists())

    def test_unmarked_composer_target_is_never_archived(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "example.com" / "public_html"
            target.mkdir(parents=True)
            (target / "index.php").write_text("<?php\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "live or foreign instance"):
                _archive_existing_composer_target(target)

            self.assertTrue((target / "index.php").exists())

    def test_source_instance_parent_is_archived_after_successful_move(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "www"
            source = base / "legacy" / "public_html"
            source.mkdir(parents=True)
            with patch.object(mautic_composer_move, "COMPOSER_ROOT", base):
                archived = _archive_source_root(source)

            self.assertFalse(source.exists())
            self.assertEqual(archived.name, "public_html")
            self.assertEqual(archived.parent.parent, base)
            self.assertTrue(archived.parent.name.endswith("-legacy"))

    def test_zip_to_composer_uses_staging_and_archives_source_after_switch(self) -> None:
        class FakeInventory:
            def __init__(self, _path: str) -> None:
                pass

            def rescan(self, _config):
                return 1

        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "www"
            source = base / "legacy" / "public_html"
            (source / "config").mkdir(parents=True)
            (source / "config" / "local.php").write_text(
                "<?php return ['tmp_path' => '" + str(source / "var" / "tmp") + "'];\n",
                encoding="utf-8",
            )
            skeleton = Path(td) / "skeleton"
            (skeleton / "bin").mkdir(parents=True)
            (skeleton / "bin" / "console").write_text("#!/usr/bin/env php\n", encoding="utf-8")
            (skeleton / "config").mkdir(parents=True)
            (skeleton / "config" / "local.php").write_text("<?php return [];\n", encoding="utf-8")
            archive = Path(td) / "skeleton.tar.gz"
            with tarfile.open(archive, "w:gz") as tf:
                tf.add(skeleton / "bin" / "console", arcname="bin/console")
                tf.add(skeleton / "config" / "local.php", arcname="config/local.php")

            def fake_download(_url, _token, output):
                Path(output).write_bytes(archive.read_bytes())

            class Config:
                mcc_token = "token"
                state_db_path = str(Path(td) / "state.sqlite")

            with (
                patch.object(mautic_composer_move, "COMPOSER_ROOT", base),
                patch.object(mautic_composer_move, "_find_site", return_value=(base / "site.conf", None)),
                patch.object(mautic_composer_move, "_preflight", return_value=[]),
                patch.object(mautic_composer_move, "_artifact_url", return_value="https://example.test/skeleton"),
                patch.object(mautic_composer_move, "_download", side_effect=fake_download),
                patch.object(mautic_composer_move, "_run", return_value=(0, "Mautic 7")),
                patch.object(mautic_composer_move, "_migrate_composer_move_crontabs", return_value={}),
                patch.object(
                    mautic_composer_move,
                    "_external_write_switched_vhost",
                    return_value={"active": "site", "backup": "backup"},
                ),
                patch("mcd_agent.inventory.InstanceInventory", FakeInventory),
                patch.object(mautic_composer_move.os, "geteuid", return_value=0),
            ):
                result = mautic_composer_move.move_zip_to_composer(
                    Config(),
                    root=str(source),
                    domain="Example-Shop.sales-snap.com",
                    mautic_major=7,
                    yes=True,
                )

            target = base / "example-shop.sales-snap.com" / "public_html"
            self.assertEqual(result["target_root"], str(target))
            self.assertTrue(target.exists())
            self.assertTrue(result["source_archive_root"].endswith("/public_html"))
            self.assertFalse(source.exists())
            self.assertEqual(result["healthcheck_tail"], ["Mautic 7"])

    def test_copy_mutable_state_to_composer_layout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "zip"
            target = base / "composer"
            (source / "config").mkdir(parents=True)
            (source / "config" / "local.php").write_text(
                "<?php return ['tmp_path' => '" + str(source / "var" / "tmp") + "'];",
                encoding="utf-8",
            )
            (source / "plugins" / "CustomBundle").mkdir(parents=True)
            (source / "plugins" / "CustomBundle" / "file.php").write_text("ok", encoding="utf-8")
            (source / "media" / "files").mkdir(parents=True)
            (source / "media" / "files" / "a.txt").write_text("asset", encoding="utf-8")
            (source / "var" / "cache").mkdir(parents=True)
            (source / "var" / "cache" / "drop").write_text("cache", encoding="utf-8")
            (source / "var" / "spool").mkdir(parents=True)
            (source / "var" / "spool" / "keep").write_text("queue", encoding="utf-8")
            target.mkdir()

            copied = _copy_mutable_state(source, target)

            self.assertIn("config/local.php", copied)
            self.assertIn("plugins", copied)
            self.assertEqual((target / "config" / "local.php").read_text(encoding="utf-8")[:5], "<?php")
            self.assertTrue((target / "docroot" / "plugins" / "CustomBundle" / "file.php").exists())
            self.assertFalse((target / "plugins" / "CustomBundle" / "file.php").exists())
            self.assertTrue((target / "docroot" / "media" / "files" / "a.txt").exists())
            self.assertTrue((target / "var" / "spool" / "keep").exists())
            self.assertFalse((target / "var" / "cache" / "drop").exists())

    def test_patch_paths_only_in_target_local_php(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "zip"
            target = base / "composer"
            (target / "config").mkdir(parents=True)
            (target / "config" / "local.php").write_text(
                "<?php return ["
                + "'upload_dir' => '"
                + str(source / "media" / "files")
                + "', 'plugins_path' => '"
                + str(source / "plugins")
                + "', 'tmp_path' => '"
                + str(source / "var" / "tmp")
                + "'];",
                encoding="utf-8",
            )

            self.assertTrue(_patch_paths_in_local_php(target, source))
            text = (target / "config" / "local.php").read_text(encoding="utf-8")
            self.assertIn(str(target / "docroot" / "media" / "files"), text)
            self.assertIn(str(target / "docroot" / "plugins"), text)
            self.assertIn(str(target / "var" / "tmp"), text)
            self.assertNotIn(str(target / "media"), text)
            self.assertNotIn(str(source), text)

    def test_patch_paths_inserts_missing_composer_path_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "zip"
            target = base / "composer"
            (target / "config").mkdir(parents=True)
            (target / "config" / "local.php").write_text(
                "<?php\n$parameters = array(\n"
                + "\t'db_driver' => 'pdo_mysql',\n"
                + "\t'upload_dir' => '"
                + str(target / "app" / ".." / "media" / "files")
                + "',\n"
                + "\t'form_upload_dir' => '"
                + str(target / "app" / ".." / "media" / "files" / "form")
                + "',\n"
                + "\t'report_temp_dir' => '"
                + str(target / "app" / ".." / "media" / "files" / "temp")
                + "',\n"
                + ");\n",
                encoding="utf-8",
            )

            self.assertTrue(_patch_paths_in_local_php(target, source))
            text = (target / "config" / "local.php").read_text(encoding="utf-8")
            self.assertIn("'upload_dir' => '" + str(target / "docroot" / "media" / "files") + "'", text)
            self.assertIn("'form_upload_dir' => '" + str(target / "docroot" / "media" / "files" / "form") + "'", text)
            self.assertIn("'contact_export_dir' => '" + str(target / "docroot" / "media" / "files" / "temp") + "'", text)
            self.assertIn("'report_temp_dir' => '" + str(target / "docroot" / "media" / "files" / "temp") + "'", text)
            self.assertIn("'tmp_path' => '" + str(target / "var" / "tmp") + "'", text)
            self.assertIn("'cache_path' => '" + str(target / "var" / "cache") + "'", text)
            self.assertIn("'log_path' => '" + str(target / "var" / "logs") + "'", text)
            self.assertNotIn("/app/../media", text)

    def test_ensure_runtime_dirs_recreates_writable_composer_state_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "composer"
            target.mkdir()

            created = _ensure_runtime_dirs(target)

            self.assertIn("var/logs", created)
            self.assertIn("var/cache", created)
            self.assertIn("var/tmp", created)
            self.assertIn("docroot/media/files/form", created)
            self.assertIn("docroot/media/files/temp", created)
            for rel in created:
                path = target / rel
                self.assertTrue(path.is_dir())
                self.assertTrue(path.stat().st_mode & 0o200)



    def test_composer_move_retired_marker_hides_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "zip"
            target = base / "composer"
            nginx_root = target / "docroot"
            source.mkdir()
            nginx_root.mkdir(parents=True)
            plan = ComposerMovePlan(
                source_root=source,
                target_root=target,
                nginx_root=nginx_root,
                domain="example.com",
                image_ref="composer6-skeleton",
                php_version="8.3",
                site_available=base / "site.conf",
                site_enabled=None,
            )

            marker = _mark_source_root_retired(plan)

            self.assertTrue(Path(marker).exists())
            self.assertTrue(_is_retired_instance_root(str(source)))
            self.assertIn(str(target), Path(marker).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
