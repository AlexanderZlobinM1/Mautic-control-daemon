from __future__ import annotations

import tempfile
import sys
import tarfile
import types
import unittest
import inspect
from io import BytesIO
from importlib import resources
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

config_stub = types.ModuleType("mcd_agent.config")
config_stub.AgentConfig = object
inventory_stub = types.ModuleType("mcd_agent.inventory")
inventory_stub.InstanceInventory = object
sys.modules.setdefault("mcd_agent.config", config_stub)
sys.modules.setdefault("mcd_agent.inventory", inventory_stub)

from mcd_agent import mautic_image_install as image_install
from mcd_agent.mautic_image_install import _nginx_web_root, _safe_extract

if sys.modules.get("mcd_agent.config") is config_stub:
    del sys.modules["mcd_agent.config"]
if sys.modules.get("mcd_agent.inventory") is inventory_stub:
    del sys.modules["mcd_agent.inventory"]


class MauticImageInstallNginxTests(unittest.TestCase):
    def test_image_install_flow_does_not_invoke_composer(self) -> None:
        flow = inspect.getsource(image_install.install_from_image).lower()

        self.assertNotIn("composer install", flow)
        self.assertNotIn("composer update", flow)
        self.assertNotIn("dump-autoload", flow)
        self.assertNotIn("platform_check", flow)
        self.assertNotIn("_preflight_requested_mail", flow)
        self.assertNotIn("_clear_mautic_cache", flow)
        self.assertNotIn("cache:clear", flow)

    def test_prepare_instance_parent_accepts_existing_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td) / "shop"
            parent.mkdir()

            image_install._prepare_instance_parent(parent / "public_html")

            self.assertTrue(parent.exists())

    def test_prepare_instance_parent_rejects_nonempty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td) / "shop"
            parent.mkdir()
            (parent / "keep.txt").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "not empty"):
                image_install._prepare_instance_parent(parent / "public_html")

    def test_failed_install_rollback_drops_only_created_database_resources(self) -> None:
        plan = image_install.build_plan(
            image_ref="default7",
            domain="shop.sales-snap.com",
            php_version="8.4",
        )
        sql: list[str] = []
        with patch.object(image_install, "_mysql_exec", side_effect=lambda statement, **_kwargs: sql.append(statement)):
            errors = image_install._rollback_failed_install(
                SimpleNamespace(),
                plan,
                database_created=True,
                user_created=True,
                resources_started=False,
                mail_requested=False,
            )

        self.assertEqual(errors, [])
        self.assertEqual(sql[0], "DROP DATABASE IF EXISTS `baza_shop`")
        self.assertEqual(sql[1], "DROP USER IF EXISTS 'korisnik_shop'@'localhost'")

    def test_composer_docroot_is_used_as_nginx_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "public_html"
            docroot = root / "docroot"
            docroot.mkdir(parents=True)
            (docroot / "index.php").write_text("<?php\n", encoding="utf-8")

            self.assertEqual(_nginx_web_root(root), docroot)

    def test_zip_root_is_used_when_no_docroot_index_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "public_html"
            root.mkdir(parents=True)
            (root / "index.php").write_text("<?php\n", encoding="utf-8")

            self.assertEqual(_nginx_web_root(root), root)

    def test_generated_nginx_vhost_is_ipv4_only(self) -> None:
        template = resources.files("mcd_agent.templates.nginx").joinpath("mautic_image_vhost.conf").read_text(encoding="utf-8")

        self.assertIn("listen 80;", template)
        self.assertNotIn("listen [::]", template)

    def test_generated_nginx_vhost_exposes_acme_before_mautic_front_controller(self) -> None:
        template = resources.files("mcd_agent.templates.nginx").joinpath("mautic_image_vhost.conf").read_text(encoding="utf-8")

        self.assertLess(
            template.index("location ^~ /.well-known/acme-challenge/"),
            template.index("{{HTTP_ROOT_LOCATION}}"),
        )
        self.assertIn("try_files $uri =404;", template)

    def test_new_image_template_has_standard_form_embed_placeholder(self) -> None:
        template = resources.files("mcd_agent.templates.nginx").joinpath("mautic_image_vhost.conf").read_text(encoding="utf-8")

        self.assertLess(template.index("{{FORM_EMBED_HTTP_LOCATION}}"), template.index("{{MAUTIC_DENY_LOCATIONS}}"))

    def test_cloudflare_dns01_certbot_does_not_put_token_on_command_line(self) -> None:
        plan = image_install.build_plan(image_ref="default7", domain="example.sales-snap.com", php_version="8.4")
        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="agent-token")
        calls: list[list[str]] = []

        def fake_run(args: list[str], **_: object) -> tuple[int, str]:
            calls.append(args)
            return 0, "ok"

        with (
            patch.object(image_install, "_ensure_certbot_cloudflare_plugin"),
            patch.object(image_install, "_certbot_cloudflare_credentials_path", return_value=Path("/etc/letsencrypt/mcd/cf.ini")),
            patch.object(image_install, "_write_nginx_vhost") as write_vhost,
            patch.object(image_install, "_run", side_effect=fake_run),
        ):
            image_install._run_certbot_cloudflare_dns01(cfg, plan, "mcc://dns/cloudflare/dns1")

        self.assertEqual(calls[0][0:2], ["certbot", "certonly"])
        self.assertIn("--dns-cloudflare", calls[0])
        self.assertIn("/etc/letsencrypt/mcd/cf.ini", calls[0])
        self.assertNotIn("agent-token", " ".join(calls[0]))
        write_vhost.assert_called_once_with(plan, ssl_enabled=True)

    def test_certbot_retries_when_global_lock_is_busy(self) -> None:
        with (
            patch.object(
                image_install,
                "_run",
                side_effect=[
                    (1, "Another instance of Certbot is already running."),
                    (0, "deleted"),
                ],
            ) as run,
            patch.object(image_install.time, "sleep") as sleep,
        ):
            rc, out = image_install._run_certbot(
                ["certbot", "delete", "--cert-name", "example.test"],
                timeout_sec=120,
            )

        self.assertEqual((rc, out), (0, "deleted"))
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(image_install._CERTBOT_LOCK_RETRY_SEC)

    def test_package_install_accepts_apt_releaseinfo_changes(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args: list[str], **_: object) -> SimpleNamespace:
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch.object(image_install.shutil, "which", return_value="/usr/bin/apt-get"),
            patch.object(image_install.subprocess, "run", side_effect=fake_run),
        ):
            image_install._apt_install_packages(["python3-certbot-dns-cloudflare"])

        self.assertEqual(calls[0], ["apt-get", "update", "--allow-releaseinfo-change"])
        self.assertEqual(
            calls[1],
            ["apt-get", "install", "-y", "--no-install-recommends", "python3-certbot-dns-cloudflare"],
        )

    def test_nginx_uses_generic_php_fpm_socket_when_requested_socket_is_missing(self) -> None:
        def fake_exists(path: Path) -> bool:
            return str(path) == "/run/php/php-fpm.sock"

        with patch.object(Path, "exists", fake_exists):
            self.assertEqual(image_install._php_fpm_fastcgi_pass("8.3"), "unix:/run/php/php-fpm.sock")

    def test_safe_extract_skips_mcd_runtime_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "image.tar.gz"
            target = Path(td) / "out"
            target.mkdir()
            with tarfile.open(archive, "w:gz") as tf:
                payload = b"<?php\n"
                info = tarfile.TarInfo("./index.php")
                info.size = len(payload)
                tf.addfile(info, BytesIO(payload))

                mcd_dir = tarfile.TarInfo("./.mcd")
                mcd_dir.type = tarfile.DIRTYPE
                tf.addfile(mcd_dir)

                mcd_link = tarfile.TarInfo("./.mcd/php")
                mcd_link.type = tarfile.SYMTYPE
                mcd_link.linkname = "/opt/mcd/generated/instances/default7/php"
                tf.addfile(mcd_link)

            with tarfile.open(archive, "r:gz") as tf:
                _safe_extract(tf, target)

            self.assertTrue((target / "index.php").exists())
            self.assertFalse((target / ".mcd").exists())

    def test_safe_extract_still_rejects_non_mcd_unsafe_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "image.tar.gz"
            target = Path(td) / "out"
            target.mkdir()
            with tarfile.open(archive, "w:gz") as tf:
                bad_link = tarfile.TarInfo("./bad")
                bad_link.type = tarfile.SYMTYPE
                bad_link.linkname = "/etc/passwd"
                tf.addfile(bad_link)

            with tarfile.open(archive, "r:gz") as tf:
                with self.assertRaisesRegex(RuntimeError, "unsafe link"):
                    _safe_extract(tf, target)

class MauticImageInstallMysqlCredentialTests(unittest.TestCase):
    def tearDown(self) -> None:
        image_install._MYSQL_ADMIN_BASE = None

    def test_mysql_exec_uses_plain_client_when_root_socket_auth_works(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args: list[str], **_: object) -> tuple[int, str]:
            calls.append(args)
            if args[-1] == "SELECT 1":
                return 0, "1"
            return 0, "ok"

        with (
            patch.object(image_install, "_mysql_bin", return_value="mysql"),
            patch.object(image_install, "_mysql_default_files", return_value=[]),
            patch.object(image_install, "_run", side_effect=fake_run),
        ):
            self.assertEqual(image_install._mysql_exec("SELECT 2"), "ok")

        self.assertEqual(calls[0], ["mysql", "-N", "-B", "-e", "SELECT 1"])
        self.assertEqual(calls[1], ["mysql", "-N", "-B", "-e", "SELECT 2"])

    def test_mysql_exec_falls_back_to_existing_defaults_file(self) -> None:
        defaults = Path("/etc/mysql/debian.cnf")
        calls: list[list[str]] = []

        def fake_run(args: list[str], **_: object) -> tuple[int, str]:
            calls.append(args)
            if args[-1] == "SELECT 1" and f"--defaults-extra-file={defaults}" not in args:
                return 1, "ERROR 1045 (28000): Access denied"
            if args[-1] == "SELECT 1":
                return 0, "1"
            return 0, "ok"

        with (
            patch.object(image_install, "_mysql_bin", return_value="mysql"),
            patch.object(image_install, "_mysql_default_files", return_value=[defaults]),
            patch.object(Path, "exists", return_value=True),
            patch.object(image_install, "_run", side_effect=fake_run),
        ):
            self.assertEqual(image_install._mysql_exec("SELECT 2"), "ok")

        self.assertEqual(calls[0], ["mysql", "-N", "-B", "-e", "SELECT 1"])
        self.assertEqual(
            calls[1],
            ["mysql", f"--defaults-extra-file={defaults}", "-N", "-B", "-e", "SELECT 1"],
        )
        self.assertEqual(
            calls[2],
            ["mysql", f"--defaults-extra-file={defaults}", "-N", "-B", "-e", "SELECT 2"],
        )

    def test_myloader_reuses_detected_defaults_file_without_inline_passwords(self) -> None:
        defaults = Path("/etc/mysql/debian.cnf")

        def fake_run(args: list[str], **_: object) -> tuple[int, str]:
            if args[-1] == "SELECT 1" and f"--defaults-extra-file={defaults}" not in args:
                return 1, "ERROR 1045 (28000): Access denied"
            return 0, "1"

        with (
            patch.object(image_install, "_mysql_bin", return_value="mysql"),
            patch.object(image_install, "_mysql_default_files", return_value=[defaults]),
            patch.object(Path, "exists", return_value=True),
            patch.object(image_install, "_myloader_bin", return_value="myloader"),
            patch.object(image_install, "_run", side_effect=fake_run),
        ):
            self.assertEqual(
                image_install._myloader_base_args(object()),
                ["myloader", f"--defaults-file={defaults}"],
            )


class MauticImageInstallSqlImportTests(unittest.TestCase):
    def test_generated_column_insert_is_rewritten_without_generated_value(self) -> None:
        sql = [
            "CREATE TABLE `ss_email_stats` (\n",
            "  `id` int NOT NULL,\n",
            "  `email_address` varchar(191) NOT NULL,\n",
            "  `date_sent` datetime NOT NULL,\n",
            "  `generated_sent_date` date GENERATED ALWAYS AS (date(`date_sent`)) VIRTUAL,\n",
            "  PRIMARY KEY (`id`)\n",
            ") ENGINE=InnoDB;\n",
            "INSERT INTO `ss_email_stats` VALUES\n",
            "(1,'a,b@example.com','2026-05-27 10:00:00','2026-05-27'),\n",
            "(2,'quote\\'d@example.com','2026-05-28 10:00:00','2026-05-28');\n",
        ]

        out = "".join(image_install._iter_mysql_import_sql(sql))

        self.assertIn(
            "INSERT INTO `ss_email_stats` (`id`,`email_address`,`date_sent`) VALUES\n",
            out,
        )
        self.assertIn("(1,'a,b@example.com','2026-05-27 10:00:00')", out)
        self.assertIn("(2,'quote\\'d@example.com','2026-05-28 10:00:00')", out)
        self.assertNotIn("'2026-05-27');", out)


if __name__ == "__main__":
    unittest.main()
