from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import mcd_agent.mautic_composer_move as mautic_composer_move
from mcd_agent.discovery import _is_retired_instance_root
from mcd_agent.mautic_composer_move import (
    ComposerMovePlan,
    _copy_mutable_state,
    _ensure_runtime_dirs,
    _image_ref_for_major,
    _mark_source_root_retired,
    _patch_paths_in_local_php,
    _php_version_for_major,
    _short,
    _write_switched_vhost,
)


class ComposerMoveHelpersTest(unittest.TestCase):
    def test_major_maps_to_skeleton_and_php(self) -> None:
        self.assertEqual(_image_ref_for_major(6), "composer6-skeleton")
        self.assertEqual(_image_ref_for_major(7), "composer7-skeleton")
        self.assertEqual(_php_version_for_major(6), "8.3")
        self.assertEqual(_php_version_for_major(7), "8.4")

    def test_short_name_uses_first_domain_label(self) -> None:
        self.assertEqual(_short("Example-Shop.sales-snap.com"), "example_shop")

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

    def test_switched_vhost_preserves_public_app_assets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "zip"
            target = base / "composer"
            nginx_root = target / "docroot"
            site = base / "site.conf"
            source.mkdir()
            nginx_root.mkdir(parents=True)
            site.write_text(
                """server {
    listen 443 ssl http2;
    server_name example.com;
    root SOURCE_ROOT;
    fastcgi_pass unix:/var/run/php/php8.2-fpm.sock;

    location ~ /app/ {
        deny all;
    }
}
""".replace("SOURCE_ROOT", str(source)),
                encoding="utf-8",
            )
            plan = ComposerMovePlan(
                source_root=source,
                target_root=target,
                nginx_root=nginx_root,
                domain="example.com",
                image_ref="composer6-skeleton",
                php_version="8.3",
                site_available=site,
                site_enabled=None,
            )

            old_http2 = mautic_composer_move._nginx_supports_http2_directive
            try:
                mautic_composer_move._nginx_supports_http2_directive = lambda: True
                _write_switched_vhost(plan)
            finally:
                mautic_composer_move._nginx_supports_http2_directive = old_http2

            text = site.read_text(encoding="utf-8")
            self.assertIn(f"root {nginx_root};", text)
            self.assertIn("fastcgi_pass unix:/var/run/php/php8.3-fpm.sock;", text)
            self.assertNotIn("php8.2-fpm.sock", text)
            self.assertIn("^/app/bundles/.*/Assets/", text)
            self.assertIn("^/app/assets/", text)
            self.assertLess(text.index("^/app/assets/"), text.index("location ~ /app/"))
            self.assertIn("http2 on;", text)

    def test_switched_vhost_converts_regular_enabled_file_to_available_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            sites_available = base / "sites-available"
            sites_enabled = base / "sites-enabled"
            source = base / "zip"
            target = base / "composer"
            nginx_root = target / "docroot"
            sites_available.mkdir()
            sites_enabled.mkdir()
            source.mkdir()
            nginx_root.mkdir(parents=True)
            enabled_site = sites_enabled / "default.sales-snap.com.conf"
            available_site = sites_available / enabled_site.name
            enabled_site.write_text(
                """server {
    listen 443 ssl;
    server_name example.com;
    root SOURCE_ROOT;
    fastcgi_pass unix:/var/run/php/php8.2-fpm.sock;
}
""".replace("SOURCE_ROOT", str(source)),
                encoding="utf-8",
            )
            available_site.write_text("server { root /old/available; }\n", encoding="utf-8")
            plan = ComposerMovePlan(
                source_root=source,
                target_root=target,
                nginx_root=nginx_root,
                domain="example.com",
                image_ref="composer6-skeleton",
                php_version="8.3",
                site_available=enabled_site,
                site_enabled=enabled_site,
            )

            old_available = mautic_composer_move.NGINX_SITES_AVAILABLE
            old_enabled = mautic_composer_move.NGINX_SITES_ENABLED
            try:
                mautic_composer_move.NGINX_SITES_AVAILABLE = sites_available
                mautic_composer_move.NGINX_SITES_ENABLED = sites_enabled
                result = _write_switched_vhost(plan)
            finally:
                mautic_composer_move.NGINX_SITES_AVAILABLE = old_available
                mautic_composer_move.NGINX_SITES_ENABLED = old_enabled

            self.assertTrue(enabled_site.is_symlink())
            self.assertEqual(enabled_site.resolve(), available_site.resolve())
            self.assertFalse(list(sites_enabled.glob("zip-backup-*")))
            self.assertTrue(list(sites_available.glob("zip-backup-*-default.sales-snap.com.conf")))
            self.assertTrue(list(sites_available.glob("pre-composer-*-default.sales-snap.com.conf")))
            text = available_site.read_text(encoding="utf-8")
            self.assertIn(f"root {nginx_root};", text)
            self.assertIn("fastcgi_pass unix:/var/run/php/php8.3-fpm.sock;", text)
            self.assertEqual(result["active"], str(available_site))
            self.assertTrue(result["backup"].startswith(str(sites_available)))

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
