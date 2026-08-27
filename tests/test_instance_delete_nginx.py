from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mcd_agent.instance_delete as instance_delete


class InstanceDeleteNginxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.enabled = root / "sites-enabled"
        self.available = root / "sites-available"
        self.enabled.mkdir()
        self.available.mkdir()
        self.old_dirs = instance_delete._NGINX_DIRS
        instance_delete._NGINX_DIRS = (self.enabled, self.available)

    def tearDown(self) -> None:
        instance_delete._NGINX_DIRS = self.old_dirs
        self.tmp.cleanup()

    def test_candidates_require_exact_domain_not_shared_root(self) -> None:
        keep_conf = self.available / "keep.sales-snap.com.conf"
        delete_conf = self.available / "delete.sales-snap.com.conf"
        root = "/var/www/shared"
        keep_conf.write_text(
            f"server {{ server_name keep.sales-snap.com; root {root}; }}\n",
            encoding="utf-8",
        )
        delete_conf.write_text(
            f"server {{ server_name delete.sales-snap.com; root {root}; }}\n",
            encoding="utf-8",
        )
        (self.enabled / keep_conf.name).symlink_to(keep_conf)
        (self.enabled / delete_conf.name).symlink_to(delete_conf)

        candidates = instance_delete._nginx_candidates(Path(root), ["delete.sales-snap.com"])

        self.assertEqual(candidates, [self.enabled / delete_conf.name, delete_conf])

    def test_delete_vhost_removes_enabled_and_available_config(self) -> None:
        conf = self.available / "site.sales-snap.com.conf"
        conf.write_text("server { server_name site.sales-snap.com; }\n", encoding="utf-8")
        enabled = self.enabled / conf.name
        enabled.symlink_to(conf)

        candidates = instance_delete._nginx_candidates(Path("/var/www/site"), ["site.sales-snap.com"])
        messages = [instance_delete._disable_nginx_vhost(path)[1] for path in candidates]

        self.assertFalse(enabled.exists() or enabled.is_symlink())
        self.assertFalse(conf.exists())
        self.assertTrue(any("enabled nginx config" in message for message in messages))
        self.assertTrue(any("available nginx config" in message for message in messages))

    def test_delete_regular_enabled_file_does_not_create_available_copy(self) -> None:
        enabled = self.enabled / "legacy.sales-snap.com.conf"
        enabled.write_text("server { server_name legacy.sales-snap.com; }\n", encoding="utf-8")

        changed, _msg = instance_delete._disable_nginx_vhost(enabled)

        self.assertTrue(changed)
        self.assertFalse(enabled.exists())
        self.assertFalse((self.available / enabled.name).exists())

    def test_delete_vhost_preserves_other_domain(self) -> None:
        keep_conf = self.available / "keep.sales-snap.com.conf"
        keep_conf.write_text("server { server_name keep.sales-snap.com; }\n", encoding="utf-8")
        keep_enabled = self.enabled / keep_conf.name
        keep_enabled.symlink_to(keep_conf)

        candidates = instance_delete._nginx_candidates(Path("/var/www/site"), ["delete.sales-snap.com"])

        self.assertEqual(candidates, [])
        self.assertTrue(keep_conf.exists())
        self.assertTrue(keep_enabled.exists())

    def test_remove_empty_instance_parent_is_strictly_scoped(self) -> None:
        root = Path(self.tmp.name)
        instance_parent = root / "shop"
        webroot = instance_parent / "public_html"
        instance_parent.mkdir()

        with patch.object(instance_delete, "_VAR_WWW", root):
            removed = instance_delete._remove_empty_instance_parent(webroot)

        self.assertTrue(removed)
        self.assertFalse(instance_parent.exists())

    def test_remove_instance_parent_preserves_nonempty_directory(self) -> None:
        root = Path(self.tmp.name)
        instance_parent = root / "shop"
        webroot = instance_parent / "public_html"
        instance_parent.mkdir()
        (instance_parent / "keep.txt").write_text("keep", encoding="utf-8")

        with patch.object(instance_delete, "_VAR_WWW", root):
            removed = instance_delete._remove_empty_instance_parent(webroot)

        self.assertFalse(removed)
        self.assertTrue(instance_parent.exists())

    def test_managed_database_user_requires_exact_image_naming(self) -> None:
        self.assertEqual(instance_delete._managed_image_db_user("baza_shop", ""), "korisnik_shop")
        self.assertEqual(instance_delete._managed_image_db_user("baza_shop", "korisnik_shop"), "korisnik_shop")
        self.assertEqual(instance_delete._managed_image_db_user("baza_shop", "custom_user"), "")
        self.assertEqual(instance_delete._managed_image_db_user("customer_db", "korisnik_customer"), "")

    def test_remove_certificate_requires_exact_single_domain(self) -> None:
        root = Path(self.tmp.name)
        live = root / "live"
        renewal = root / "renewal"
        creds = root / "mcd"
        (live / "site.sales-snap.com").mkdir(parents=True)
        renewal.mkdir()
        creds.mkdir()
        credential = creds / "dns-cloudflare-site.sales-snap.com.ini"
        credential.write_text("secret", encoding="utf-8")
        calls: list[list[str]] = []

        def fake_run(args: list[str], **_kwargs: object) -> tuple[int, str]:
            calls.append(args)
            if args[1] == "certificates":
                return 0, "Domains: site.sales-snap.com"
            return 0, "deleted"

        with (
            patch.object(instance_delete, "_LETSENCRYPT_LIVE", live),
            patch.object(instance_delete, "_LETSENCRYPT_RENEWAL", renewal),
            patch.object(instance_delete, "_LETSENCRYPT_MCD", creds),
            patch.object(instance_delete, "_run", side_effect=fake_run),
        ):
            changed, _message = instance_delete._remove_dedicated_certificate("site.sales-snap.com")

        self.assertTrue(changed)
        self.assertEqual(calls[1][0:3], ["certbot", "delete", "--cert-name"])
        self.assertFalse(credential.exists())

    def test_remove_certificate_preserves_shared_certificate(self) -> None:
        root = Path(self.tmp.name)
        live = root / "live"
        (live / "site.sales-snap.com").mkdir(parents=True)
        with (
            patch.object(instance_delete, "_LETSENCRYPT_LIVE", live),
            patch.object(instance_delete, "_LETSENCRYPT_RENEWAL", root / "renewal"),
            patch.object(instance_delete, "_run", return_value=(0, "Domains: site.sales-snap.com alias.example.com")) as run,
        ):
            changed, message = instance_delete._remove_dedicated_certificate("site.sales-snap.com")

        self.assertFalse(changed)
        self.assertIn("not dedicated", message)
        self.assertEqual(run.call_count, 1)

    def test_remove_instance_root_retries_after_directory_not_empty(self) -> None:
        root = Path(self.tmp.name) / "public_html"
        (root / ".mcd").mkdir(parents=True)
        (root / ".mcd" / "mautic.version").write_text("7.1.2\n", encoding="utf-8")
        original_rmtree = shutil.rmtree
        calls = 0

        def flaky_rmtree(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("[Errno 39] Directory not empty")
            original_rmtree(path)

        with patch.object(instance_delete.shutil, "rmtree", side_effect=flaky_rmtree):
            instance_delete._remove_instance_root(root, attempts=3, sleep_sec=0)

        self.assertGreaterEqual(calls, 2)
        self.assertFalse(root.exists())

    def test_remove_instance_root_deletes_root_recreated_after_quarantine(self) -> None:
        root = Path(self.tmp.name) / "public_html"
        (root / ".mcd").mkdir(parents=True)
        (root / ".mcd" / "mautic.version").write_text("7.1.2\n", encoding="utf-8")
        original_rmtree = shutil.rmtree
        calls = 0

        def rmtree_and_recreate(path: Path) -> None:
            nonlocal calls
            calls += 1
            original_rmtree(path)
            if calls == 1:
                (root / ".mcd").mkdir(parents=True)
                (root / ".mcd" / "mautic.version").write_text("7.1.2\n", encoding="utf-8")

        with patch.object(instance_delete.shutil, "rmtree", side_effect=rmtree_and_recreate):
            instance_delete._remove_instance_root(root, attempts=3, sleep_sec=0)

        self.assertGreaterEqual(calls, 2)
        self.assertFalse(root.exists())

    def test_remove_instance_root_reports_remaining_entries(self) -> None:
        root = Path(self.tmp.name) / "public_html"
        (root / ".mcd").mkdir(parents=True)
        (root / ".mcd" / "mautic.version").write_text("7.1.2\n", encoding="utf-8")

        with patch.object(instance_delete.shutil, "rmtree", side_effect=OSError("[Errno 39] Directory not empty")):
            with self.assertRaisesRegex(RuntimeError, r"mautic\.version"):
                instance_delete._remove_instance_root(root, attempts=2, sleep_sec=0)


if __name__ == "__main__":
    unittest.main()
