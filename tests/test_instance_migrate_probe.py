from __future__ import annotations

import gzip
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from importlib import resources

from mcd_agent.models import DBConfig, MauticInstall
import mcd_agent.instance_migrate as instance_migrate


class InstanceMigrateProbeTests(unittest.TestCase):
    def test_select_instance_accepts_domain_alias(self) -> None:
        inst = MauticInstall(
            instance_uid="uid-1",
            name="example.sales-snap.com",
            root="/var/www/example/public_html",
            console_path="/var/www/example/public_html/bin/console",
            primary_domain="example.sales-snap.com",
            domains=["alias.sales-snap.com"],
        )

        self.assertIs(instance_migrate._select_instance([inst], "alias.sales-snap.com"), inst)
        self.assertIs(instance_migrate._select_instance([inst], "/var/www/example/public_html"), inst)

    def test_database_probe_requires_local_binlog_for_catchup(self) -> None:
        inst = MauticInstall(
            instance_uid="uid-1",
            name="example",
            root="/var/www/example",
            console_path="/var/www/example/bin/console",
            db=DBConfig(
                host="db.example.net",
                port=3306,
                name="mautic",
                user="u",
                password="secret",
                table_prefix="",
            ),
        )

        class FakeDB:
            def __init__(self, _cfg: DBConfig) -> None:
                pass

            def fetch_rows(self, query: str, limit: int = 5000, context=None):
                if "information_schema.TABLES" in query:
                    return [{"size_bytes": 12345}]
                if "super_read_only" in query:
                    return [{"super_read_only": 0}]
                return [
                    {
                        "version": "8.0.36",
                        "version_comment": "MySQL Community Server",
                        "log_bin": 0,
                        "binlog_format": "ROW",
                        "server_id": 0,
                        "read_only": 0,
                    }
                ]

        with patch.object(instance_migrate, "MauticDB", FakeDB):
            payload = instance_migrate._database_probe(inst)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["engine"], "mysql")
        self.assertEqual(payload["size_bytes"], 12345)
        self.assertFalse(payload["catchup_supported"])
        self.assertIn("db_not_local", payload["catchup_blockers"])
        self.assertIn("source_binlog_disabled", payload["catchup_blockers"])
        self.assertIn("source_server_id_missing", payload["catchup_blockers"])

    def test_source_db_tunnel_puts_ssh_options_before_destination(self) -> None:
        class FakeProc:
            stdout = None
            stderr = None

            def poll(self):
                return None

        captured: dict[str, list[str]] = {}

        def fake_popen(argv, **_kwargs):
            captured["argv"] = list(argv)
            return FakeProc()

        with (
            patch.object(instance_migrate, "_find_free_local_port", return_value=12345),
            patch.object(instance_migrate, "_run", return_value=(0, "")),
            patch.object(instance_migrate.subprocess, "Popen", side_effect=fake_popen),
        ):
            proc, port = instance_migrate._open_source_db_tunnel(
                source_ssh_user="root",
                source_address="192.0.2.10",
                source_ssh_port=9797,
                source_ssh_key_file="/tmp/key",
                source_db_port=3306,
            )

        self.assertIsInstance(proc, FakeProc)
        self.assertEqual(port, 12345)
        argv = captured["argv"]
        self.assertEqual(argv[-1], "root@192.0.2.10")
        self.assertLess(argv.index("-N"), argv.index("root@192.0.2.10"))
        self.assertLess(argv.index("-L"), argv.index("root@192.0.2.10"))

    def test_patch_local_php_uses_target_database_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_dir = root / "config"
            cfg_dir.mkdir()
            (cfg_dir / "local.php").write_text(
                "<?php return array('db_host' => 'localhost', 'db_port' => '3306', "
                "'db_name' => 'baza_ss', 'db_user' => 'korisnik_ss', 'db_password' => 'old');",
                encoding="utf-8",
            )
            db = DBConfig(
                host="localhost",
                port=3306,
                name="baza_3gstore",
                user="korisnik_3gstore",
                password="newsecret",
                table_prefix="",
            )

            instance_migrate._patch_local_php_db(root, db)

            text = (cfg_dir / "local.php").read_text(encoding="utf-8")
            self.assertIn("'db_name' => 'baza_3gstore'", text)
            self.assertIn("'db_user' => 'korisnik_3gstore'", text)
            self.assertIn("'db_password' => 'newsecret'", text)

    def test_patch_local_php_instance_paths_are_target_local(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "target" / "public_html"
            cfg_dir = root / "config"
            cfg_dir.mkdir(parents=True)
            (root / "index.php").write_text("<?php\n", encoding="utf-8")
            (cfg_dir / "local.php").write_text(
                "<?php return array("
                "'cache_path' => '/var/www/mautic/var/cache', "
                "'log_path' => '/var/www/mautic/var/logs', "
                "'tmp_path' => '/var/www/mautic/var/tmp', "
                "'import_campaigns_dir' => '/var/www/mautic/var/import', "
                "'import_leads_dir' => '/var/www/mautic/var/import', "
                "'upload_dir' => '/var/www/mautic/media/files', "
                "'contact_export_dir' => '/var/www/mautic/media/files/temp', "
                "'report_temp_dir' => '/var/www/mautic/media/files/temp', "
                "'form_upload_dir' => '/var/www/mautic/media/files/form');",
                encoding="utf-8",
            )

            changed = instance_migrate._patch_local_php_instance_paths(root)

            resolved = root.resolve()
            text = (cfg_dir / "local.php").read_text(encoding="utf-8")
            self.assertIn("cache_path' => '" + str(resolved / "var" / "cache") + "'", text)
            self.assertIn("log_path' => '" + str(resolved / "var" / "logs") + "'", text)
            self.assertIn("tmp_path' => '" + str(resolved / "var" / "tmp") + "'", text)
            self.assertIn("upload_dir' => '" + str(resolved / "media" / "files") + "'", text)
            self.assertIn("form_upload_dir' => '" + str(resolved / "media" / "files" / "form") + "'", text)
            self.assertNotIn("/var/www/mautic", text)
            self.assertIn("cache_path", changed)
            self.assertTrue((resolved / "var" / "cache").is_dir())
            self.assertTrue((resolved / "media" / "files" / "temp").is_dir())

    def test_target_relay_preflight_reports_existing_target_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "public_html"
            root.mkdir()
            (root / "index.php").write_text("<?php\n", encoding="utf-8")

            with (
                patch.object(instance_migrate, "_target_db_exists", return_value=True),
                patch.object(instance_migrate.shutil, "which", return_value="/usr/bin/tool"),
            ):
                payload = instance_migrate.preflight_target_relay(
                    target_root=str(root),
                    target_db_name="baza_testmove",
                )

        self.assertFalse(payload["ok"])
        self.assertTrue(any("target root already exists" in x for x in payload["problems"]))
        self.assertTrue(any("target database already exists" in x for x in payload["problems"]))

    def test_target_direct_db_requires_password(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "target database password is required"):
            instance_migrate._target_db_from_direct_values(name="baza_test", user="korisnik_test", password="")

    def test_target_relay_preflight_can_clean_selected_target_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "target" / "public_html"
            root.mkdir(parents=True)
            (root / "index.php").write_text("<?php\n", encoding="utf-8")
            sql: list[str] = []

            with (
                patch.object(instance_migrate, "_target_db_exists", return_value=True),
                patch.object(instance_migrate, "_mysql_exec", side_effect=lambda q: sql.append(q) or ""),
                patch.object(instance_migrate.shutil, "which", return_value="/usr/bin/tool"),
            ):
                payload = instance_migrate.preflight_target_relay(
                    target_root=str(root),
                    target_db_name="baza_testmove",
                    wipe_target_root=True,
                    wipe_target_db=True,
                )

        self.assertTrue(payload["ok"])
        self.assertFalse(root.exists())
        self.assertTrue(any("DROP DATABASE IF EXISTS `baza_testmove`" in q for q in sql))
        self.assertIn("target root removed: " + str(root.resolve()), payload["cleanup"])
        self.assertIn("target database dropped: baza_testmove", payload["cleanup"])

    def test_letsencrypt_stream_has_safe_relative_members(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            live = Path(td) / "etc" / "letsencrypt" / "live" / "example.com"
            live.mkdir(parents=True)
            (live / "fullchain.pem").write_text("cert", encoding="utf-8")
            buf = io.BytesIO()

            with patch.object(instance_migrate, "_letsencrypt_paths_for_domains", return_value=[live]):
                instance_migrate.stream_source_letsencrypt(domains_json='["example.com"]', output=buf)

            buf.seek(0)
            with tarfile.open(fileobj=buf, mode="r:gz") as tf:
                names = tf.getnames()

        self.assertTrue(names)
        self.assertTrue(all(not name.startswith("/") for name in names))
        self.assertTrue(any(name.endswith("fullchain.pem") for name in names))

    def test_letsencrypt_paths_include_certificate_named_for_another_domain_when_san_matches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "letsencrypt"
            cert_name = "dijasporashop.sales-snap.com"
            live = root / "live" / cert_name
            archive = root / "archive" / cert_name
            renewal = root / "renewal" / f"{cert_name}.conf"
            live.mkdir(parents=True)
            archive.mkdir(parents=True)
            (live / "fullchain.pem").write_text("cert", encoding="utf-8")
            (live / "privkey.pem").write_text("key", encoding="utf-8")
            (archive / "fullchain1.pem").write_text("cert", encoding="utf-8")
            renewal.parent.mkdir(parents=True, exist_ok=True)
            renewal.write_text("renewal", encoding="utf-8")

            with (
                patch.object(instance_migrate, "LETSENCRYPT_ROOT", root),
                patch.object(instance_migrate, "_certificate_dns_names", return_value={"winwin.sales-snap.com"}),
            ):
                paths = instance_migrate._letsencrypt_paths_for_domains(["winwin.sales-snap.com"])

        self.assertEqual(paths, [live, archive, renewal])

    def test_write_nginx_vhost_uses_current_san_matched_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app_root = root / "app" / "public_html"
            (app_root / "docroot").mkdir(parents=True)
            (app_root / "docroot" / "index.php").write_text("<?php\n", encoding="utf-8")
            cert_live = root / "letsencrypt" / "live" / "shared-certificate"
            cert_live.mkdir(parents=True)
            (cert_live / "fullchain.pem").write_text("cert", encoding="utf-8")
            (cert_live / "privkey.pem").write_text("key", encoding="utf-8")
            php_socket = root / "php8.3-fpm.sock"
            php_socket.touch()
            available = root / "sites-available"
            enabled = root / "sites-enabled"
            available.mkdir()
            enabled.mkdir()

            old_available = instance_migrate.NGINX_SITES_AVAILABLE
            old_enabled = instance_migrate.NGINX_SITES_ENABLED
            try:
                instance_migrate.NGINX_SITES_AVAILABLE = available
                instance_migrate.NGINX_SITES_ENABLED = enabled
                with (
                    patch.object(instance_migrate, "LETSENCRYPT_ROOT", root / "letsencrypt"),
                    patch.object(instance_migrate, "_certificate_dns_names", return_value={"winwin.sales-snap.com"}),
                    patch.object(instance_migrate, "_certificate_is_current", return_value=True),
                    patch.object(instance_migrate, "_ensure_nginx_sites_layout"),
                    patch.object(instance_migrate, "_detect_php_version", return_value="8.3"),
                    patch.object(instance_migrate, "_php_fpm_socket", return_value=php_socket),
                    patch.object(instance_migrate, "_run_checked", return_value=""),
                    patch.object(instance_migrate, "_nginx_supports_http2_directive", return_value=False),
                ):
                    site = instance_migrate._write_nginx_vhost(
                        root=app_root,
                        domains=["winwin.sales-snap.com"],
                        php_version="8.3",
                    )
            finally:
                instance_migrate.NGINX_SITES_AVAILABLE = old_available
                instance_migrate.NGINX_SITES_ENABLED = old_enabled

            content = Path(site).read_text(encoding="utf-8")

        self.assertIn("listen 443 ssl", content)
        self.assertIn(f"ssl_certificate {cert_live / 'fullchain.pem'};", content)
        self.assertIn(f"ssl_certificate_key {cert_live / 'privkey.pem'};", content)
        self.assertIn("# BEGIN MCD managed form embed", content)
        self.assertIn("frame-ancestors 'self'", content)

    def test_target_finalize_blocks_dns_cutover_without_current_certificate(self) -> None:
        db = DBConfig(
            host="localhost",
            port=3306,
            name="baza_target",
            user="target_user",
            password="target_password",
            table_prefix="",
        )
        with patch.object(instance_migrate, "_target_db_from_direct_values", return_value=db), patch.object(
            instance_migrate, "_matching_live_certificate", return_value=None
        ):
            with self.assertRaisesRegex(RuntimeError, "DNS cutover blocked"):
                instance_migrate.finalize_target_relay(
                    type("Cfg", (), {"state_db_path": "/tmp/mcd-state.sqlite"})(),
                    target_root="/var/www/example/public_html",
                    target_db_name="baza_target",
                    target_db_user="target_user",
                    target_db_password="target_password",
                    domains_json='["example.sales-snap.com"]',
                )

    def test_safe_tar_member_target_rejects_traversal(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsafe tar member path"):
            instance_migrate._safe_tar_member_target(Path("/"), "../etc/passwd")

    def test_nginx_web_root_prefers_mautic_docroot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "docroot").mkdir()
            (root / "docroot" / "index.php").write_text("<?php\n", encoding="utf-8")

            self.assertEqual(instance_migrate._nginx_web_root(root), root / "docroot")

    def test_target_pull_migration_imports_database_once_after_final_sync(self) -> None:
        class FakeInventory:
            def __init__(self, _path: str) -> None:
                pass

            def rescan(self, _config):
                return []

        class FakeProc:
            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        events: list[str] = []
        source_db = DBConfig(
            host="localhost",
            port=3306,
            name="baza_source",
            user="source_user",
            password="source_password",
            table_prefix="ss_",
        )
        target_db = DBConfig(
            host="localhost",
            port=3306,
            name="baza_target",
            user="target_user",
            password="target_password",
            table_prefix="ss_",
        )

        def fake_rsync(**kwargs):
            events.append("rsync")
            Path(kwargs["target_path"]).mkdir(parents=True, exist_ok=True)

        def fake_remote_mcd(**kwargs):
            args = list(kwargs.get("args") or [])
            if "off" in args:
                events.append("maintenance_off")
            else:
                events.append("maintenance_on")
            return "{}"

        def fake_tunnel(**_kwargs):
            events.append("tunnel")
            return FakeProc(), 3307

        def fake_dump(_config, *, local_source_db, target_db, label: str):
            events.append(f"db_import:{label}")
            return {"label": label}

        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / "source.key"
            key.write_text("key", encoding="utf-8")
            target_root = Path(td) / "target" / "public_html"
            cfg = type("Cfg", (), {"state_db_path": str(Path(td) / "state.sqlite")})()

            with (
                patch.object(instance_migrate.os, "geteuid", return_value=0),
                patch.object(instance_migrate.shutil, "which", return_value="/usr/bin/rsync"),
                patch.object(instance_migrate, "_mysql_admin_base", return_value=["mysql"]),
                patch.object(instance_migrate, "_run_checked", return_value=""),
                patch.object(instance_migrate, "_rsync_from_source", side_effect=fake_rsync),
                patch.object(instance_migrate, "_db_from_local_php", return_value=source_db),
                patch.object(instance_migrate, "_target_db_from_values", return_value=target_db),
                patch.object(instance_migrate, "_target_db_exists", return_value=False),
                patch.object(instance_migrate, "_remote_mcd", side_effect=fake_remote_mcd),
                patch.object(instance_migrate, "_open_source_db_tunnel", side_effect=fake_tunnel),
                patch.object(instance_migrate, "_dump_source_into_target", side_effect=fake_dump),
                patch.object(instance_migrate, "_patch_local_php_db", side_effect=lambda *_args: events.append("patch_db")),
                patch.object(instance_migrate, "_patch_local_php_instance_paths", side_effect=lambda *_args: events.append("patch_paths") or []),
                patch.object(instance_migrate, "_copy_letsencrypt", return_value=False),
                patch.object(instance_migrate, "_write_nginx_vhost", return_value="/etc/nginx/sites-available/example.conf"),
                patch.object(instance_migrate, "_target_healthcheck", return_value=[]),
                patch.object(instance_migrate, "InstanceInventory", FakeInventory),
            ):
                result = instance_migrate.run_target_pull_migration(
                    cfg,
                    source_address="192.0.2.10",
                    source_ssh_user="root",
                    source_ssh_port=22,
                    source_ssh_key_file=str(key),
                    source_root="/var/www/source/public_html",
                    target_root=str(target_root),
                    domains_json='["example.com"]',
                    target_db_name="baza_target",
                    target_db_user="target_user",
                    target_db_password="target_password",
                    php_version="8.4",
                )

        self.assertTrue(result["ok"])
        self.assertFalse(result["source_maintenance_active"])
        self.assertTrue(result["source_maintenance_restored"])
        self.assertEqual([x for x in events if x.startswith("db_import:")], ["db_import:single"])
        self.assertLess(events.index("maintenance_on"), events.index("db_import:single"))
        self.assertLess(events.index("db_import:single"), events.index("patch_db"))
        self.assertLess(events.index("patch_db"), events.index("maintenance_off"))

    def test_nginx_sites_layout_is_created_for_target_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            available = root / "sites-available"
            enabled = root / "sites-enabled"

            old_available = instance_migrate.NGINX_SITES_AVAILABLE
            old_enabled = instance_migrate.NGINX_SITES_ENABLED
            try:
                instance_migrate.NGINX_SITES_AVAILABLE = available
                instance_migrate.NGINX_SITES_ENABLED = enabled
                with patch.object(instance_migrate, "ensure_nginx_baseline", return_value={"status": "ok"}):
                    instance_migrate._ensure_nginx_sites_layout()

                self.assertTrue(available.is_dir())
                self.assertTrue(enabled.is_dir())
            finally:
                instance_migrate.NGINX_SITES_AVAILABLE = old_available
                instance_migrate.NGINX_SITES_ENABLED = old_enabled

    def test_generated_target_nginx_vhost_is_ipv4_only(self) -> None:
        template = resources.files("mcd_agent.templates.nginx").joinpath("instance_migrate_vhost.conf").read_text(encoding="utf-8")

        self.assertIn("{{LISTEN_DIRECTIVES}}", template)
        self.assertNotIn("listen [::]", template)

    def test_source_db_stream_prefers_mariadb_dump_without_events(self) -> None:
        class FakeInventory:
            def __init__(self, _path: str) -> None:
                pass

            def list_instances(self):
                return [
                    MauticInstall(
                        instance_uid="uid-1",
                        name="example",
                        root="/var/www/example",
                        console_path="/var/www/example/bin/console",
                    )
                ]

        class FakeProc:
            def __init__(self) -> None:
                self.stdout = io.BytesIO(b"-- sql\n")
                self.stderr = io.BytesIO(b"")

            def wait(self):
                return 0

        captured: dict[str, list[str]] = {}

        def fake_popen(argv, **_kwargs):
            captured["argv"] = list(argv)
            return FakeProc()

        cfg = type("Cfg", (), {"state_db_path": "/tmp/mcd-state.sqlite"})()
        db = DBConfig(
            host="localhost",
            port=3306,
            name="baza_source",
            user="source_user",
            password="source_password",
            table_prefix="ss_",
        )

        with (
            patch.object(instance_migrate, "InstanceInventory", FakeInventory),
            patch.object(instance_migrate, "ensure_seeded", return_value=None),
            patch.object(instance_migrate, "_db_from_local_php", return_value=db),
            patch.object(
                instance_migrate.shutil,
                "which",
                side_effect=lambda name: "/usr/bin/mariadb-dump" if name == "mariadb-dump" else "/usr/bin/mysqldump",
            ),
            patch.object(instance_migrate.subprocess, "Popen", side_effect=fake_popen),
        ):
            output = io.BytesIO()
            instance_migrate.stream_source_db(cfg, selector="/var/www/example", output=output)

        argv = captured["argv"]
        self.assertEqual(argv[0], "/usr/bin/mariadb-dump")
        self.assertIn("--routines", argv)
        self.assertIn("--triggers", argv)
        self.assertNotIn("--events", argv)

    def test_target_db_stream_rewrites_mysql8_date_format_generated_columns(self) -> None:
        sql = (
            "CREATE TABLE `ss_campaign_leads` (\n"
            "  `date_added` datetime DEFAULT NULL,\n"
            "  `generated_date_added_hour` datetime GENERATED ALWAYS AS "
            "(date_format(`date_added`,_utf8mb4'%Y-%m-%d %H:00')) STORED COMMENT '(DC2Type:generated)',\n"
            "  `generated_date_added_day` date GENERATED ALWAYS AS "
            "(date_format(`date_added`,_utf8mb4'%Y-%m-%d')) STORED COMMENT '(DC2Type:generated)',\n"
            "  `generated_date_added_week` char(7) GENERATED ALWAYS AS "
            "(date_format(`date_added`,_utf8mb4'%Y %U')) STORED COMMENT '(DC2Type:generated)',\n"
            "  `generated_date_added_month` char(7) GENERATED ALWAYS AS "
            "(date_format(`date_added`,_utf8mb4'%Y-%m')) STORED COMMENT '(DC2Type:generated)',\n"
            "  `generated_date_added_year` year GENERATED ALWAYS AS "
            "(date_format(`date_added`,_utf8mb4'%Y')) STORED COMMENT '(DC2Type:generated)'\n"
            ") ENGINE=InnoDB;\n"
        )
        gzipped = io.BytesIO()
        with gzip.GzipFile(fileobj=gzipped, mode="wb") as gz:
            gz.write(sql.encode("utf-8"))
        gzipped.seek(0)

        class FakeStdin(io.BytesIO):
            def close(self) -> None:
                pass

        fake_stdin = FakeStdin()

        class FakeProc:
            def __init__(self) -> None:
                self.stdin = fake_stdin
                self.stdout = io.BytesIO(b"")
                self.stderr = io.BytesIO(b"")
                self.returncode = 0

            def communicate(self):
                self.returncode = 0
                return b"", b""

            def kill(self):
                self.returncode = -9

        proc = FakeProc()

        with (
            patch.object(instance_migrate, "_prepare_target_db", return_value=None),
            patch.object(instance_migrate, "_mysql_admin_base", return_value=["mysql"]),
            patch.object(instance_migrate.subprocess, "Popen", return_value=proc),
        ):
            result = instance_migrate.import_target_db_stream(
                target_db_name="baza_target",
                target_db_user="korisnik_target",
                target_db_password="target_password",
                input_stream=gzipped,
            )

        imported_sql = fake_stdin.getvalue().decode("utf-8")
        self.assertTrue(result["ok"])
        self.assertNotIn("date_format(", imported_sql)
        self.assertIn("timestamp(date(`date_added`), maketime(hour(`date_added`),0,0))", imported_sql)
        self.assertIn("date(`date_added`)", imported_sql)
        self.assertIn("concat(year(`date_added`),' ',lpad(week(`date_added`,0),2,'0'))", imported_sql)
        self.assertIn("concat(year(`date_added`),'-',lpad(month(`date_added`),2,'0'))", imported_sql)
        self.assertIn("year(`date_added`)", imported_sql)


if __name__ == "__main__":
    unittest.main()
