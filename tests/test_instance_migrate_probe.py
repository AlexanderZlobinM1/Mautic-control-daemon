from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from importlib import resources

from mcd_agent.models import DBConfig, MauticInstall
import mcd_agent.instance_migrate as instance_migrate


def _complete_db_stream(sql: bytes, stream_id: str = "a" * 32) -> io.BytesIO:
    payload = io.BytesIO()
    with gzip.GzipFile(fileobj=payload, mode="wb") as gz:
        gz.write(instance_migrate._db_stream_marker("BEGIN", stream_id))
        gz.write(sql)
        gz.write(b"\n")
        gz.write(instance_migrate._db_stream_marker("COMPLETE", stream_id))
    payload.seek(0)
    return payload


class InstanceMigrateProbeTests(unittest.TestCase):
    def test_migration_cli_exposes_target_certificate_and_removes_certificate_transfer(self) -> None:
        from mcd_agent.cli import _build_parser

        parser = _build_parser()
        parsed = parser.parse_args(
            [
                "instance-migrate",
                "target-issue-certificate",
                "--target-root",
                "/var/www/example/public_html",
                "--domains-json",
                '["example.sales-snap.com"]',
                "--instance-uid",
                "a1179058-985d-4117-91b8-e951fa734a66",
                "--instance-code",
                "00007",
            ]
        )

        self.assertEqual(parsed.op, "target-issue-certificate")
        for source_op in ("source-quiesce", "source-restore", "source-finalize"):
            parsed_source = parser.parse_args(
                [
                    "instance-migrate",
                    source_op,
                    "--root",
                    "/var/www/example/public_html",
                    "--domains-json",
                    '["example.sales-snap.com"]',
                    "--instance-uid",
                    "a1179058-985d-4117-91b8-e951fa734a66",
                    "--instance-code",
                    "00007",
                ]
            )
            self.assertEqual(parsed_source.op, source_op)
        for removed in ("source-stream-letsencrypt", "target-receive-letsencrypt"):
            with self.assertRaises(SystemExit), patch("sys.stderr", new=io.StringIO()):
                parser.parse_args(["instance-migrate", removed])

    def test_new_var_www_target_must_use_full_fqdn(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "www"
            target = root / "short" / "public_html"
            with (
                patch.object(instance_migrate, "MIGRATION_ROOT", root),
                patch.object(instance_migrate, "_target_db_exists", return_value=False),
                patch.object(instance_migrate.shutil, "which", return_value="/usr/bin/tool"),
            ):
                payload = instance_migrate.preflight_target_relay(
                    target_root=str(target),
                    target_db_name="baza_testmove",
                    domains_json='["full.example.com"]',
                )

        self.assertFalse(payload["ok"])
        self.assertTrue(any("canonical full-FQDN path" in x for x in payload["problems"]))
        self.assertFalse(target.exists())

    def test_failed_migration_target_is_archived_only_when_marked_owned(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "www"
            target = root / "full.example.com" / "public_html"
            target.mkdir(parents=True)
            instance_migrate._write_migration_target_marker(
                target,
                domains=["full.example.com"],
                source_root="/var/www/source/public_html",
            )
            with (
                patch.object(instance_migrate, "MIGRATION_ROOT", root),
                patch.object(instance_migrate, "_target_db_exists", return_value=False),
                patch.object(instance_migrate.shutil, "which", return_value="/usr/bin/tool"),
            ):
                payload = instance_migrate.preflight_target_relay(
                    target_root=str(target),
                    target_db_name="baza_testmove",
                    domains_json='["full.example.com"]',
                )

        self.assertTrue(payload["ok"])
        self.assertFalse(target.exists())
        self.assertTrue(any("target root archived" in x for x in payload["cleanup"]))

    def test_unmarked_live_target_is_not_archived_or_removed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "www"
            target = root / "full.example.com" / "public_html"
            target.mkdir(parents=True)
            sentinel = target / "index.php"
            sentinel.write_text("<?php\n", encoding="utf-8")
            with (
                patch.object(instance_migrate, "MIGRATION_ROOT", root),
                patch.object(instance_migrate, "_target_db_exists", return_value=False),
                patch.object(instance_migrate.shutil, "which", return_value="/usr/bin/tool"),
            ):
                payload = instance_migrate.preflight_target_relay(
                    target_root=str(target),
                    target_db_name="baza_testmove",
                    domains_json='["full.example.com"]',
                )
            sentinel_survived = sentinel.exists()

        self.assertFalse(payload["ok"])
        self.assertTrue(sentinel_survived)
        self.assertFalse(any("target root archived" in x for x in payload["cleanup"]))

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
        self.assertEqual(payload["name"], "mautic")
        self.assertEqual(payload["user"], "u")
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

    def test_write_nginx_vhost_is_http_only_before_dns_cutover(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app_root = root / "app" / "public_html"
            (app_root / "docroot").mkdir(parents=True)
            (app_root / "docroot" / "index.php").write_text("<?php\n", encoding="utf-8")
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

        self.assertIn("listen 80;", content)
        self.assertNotIn("listen 443", content)
        self.assertNotIn("ssl_certificate", content)
        self.assertNotIn("/etc/letsencrypt", content)
        self.assertIn("# BEGIN MCD managed form embed", content)
        self.assertIn("frame-ancestors 'self'", content)

    def test_target_finalize_prepares_http_vhost_without_source_certificate(self) -> None:
        db = DBConfig(
            host="localhost",
            port=3306,
            name="baza_target",
            user="target_user",
            password="target_password",
            table_prefix="",
        )
        class FakeInventory:
            def __init__(self, _path: str) -> None:
                pass

            def rescan(self, _config):
                return []

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "public_html"
            target.mkdir()
            instance_migrate._write_migration_target_marker(
                target,
                domains=["example.sales-snap.com"],
                instance_uid="a1179058-985d-4117-91b8-e951fa734a66",
                instance_code="00007",
            )
            with (
                patch.object(instance_migrate, "_target_db_from_direct_values", return_value=db),
                patch.object(instance_migrate, "_patch_local_php_db"),
                patch.object(instance_migrate, "_patch_local_php_instance_paths"),
                patch.object(instance_migrate, "_write_nginx_vhost", return_value="/etc/nginx/sites-available/example.conf"),
                patch.object(instance_migrate, "_run_checked", return_value=""),
                patch.object(instance_migrate, "_target_healthcheck", return_value=["ok"]),
                patch.object(instance_migrate, "write_instance_identity"),
                patch.object(instance_migrate, "identity_readback", return_value={"status": "ok"}),
                patch.object(instance_migrate, "InstanceInventory", FakeInventory),
            ):
                result = instance_migrate.finalize_target_relay(
                    type("Cfg", (), {"state_db_path": str(Path(td) / "state.sqlite")})(),
                    target_root=str(target),
                    target_db_name="baza_target",
                    target_db_user="target_user",
                    target_db_password="target_password",
                    domains_json='["example.sales-snap.com"]',
                    instance_uid="a1179058-985d-4117-91b8-e951fa734a66",
                    instance_code="00007",
                )

            marker = instance_migrate._read_migration_target_marker(target)

        self.assertTrue(result["ok"])
        self.assertEqual(result["phase"], "target_prepared")
        self.assertFalse(result["migration_complete"])
        self.assertTrue(result["dns_cutover_ready"])
        self.assertTrue(result["tls_pending"])
        self.assertFalse(result["source_certificate_used"])
        self.assertEqual(result["next_operation"], "target-issue-certificate")
        self.assertEqual(marker["phase"], "target_prepared")

    def test_target_finalize_rejects_identity_mismatch_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "public_html"
            target.mkdir()
            instance_migrate._write_migration_target_marker(
                target,
                domains=["example.sales-snap.com"],
                instance_uid="a1179058-985d-4117-91b8-e951fa734a66",
                instance_code="00007",
            )
            with (
                patch.object(instance_migrate, "_patch_local_php_db") as patch_db,
                patch.object(instance_migrate, "_write_nginx_vhost") as write_vhost,
                self.assertRaisesRegex(RuntimeError, "UID does not match"),
            ):
                instance_migrate.finalize_target_relay(
                    type("Cfg", (), {"state_db_path": str(Path(td) / "state.sqlite")})(),
                    target_root=str(target),
                    target_db_name="baza_target",
                    target_db_user="target_user",
                    target_db_password="target_password",
                    domains_json='["example.sales-snap.com"]',
                    instance_uid="different-uid",
                    instance_code="00007",
                )

            patch_db.assert_not_called()
            write_vhost.assert_not_called()

    def test_target_certificate_success_requires_source_finalize(self) -> None:
        class FakeInventory:
            def __init__(self, _path: str) -> None:
                pass

            def rescan(self, _config):
                return ["instance"]

        uid = "a1179058-985d-4117-91b8-e951fa734a66"
        code = "00007"
        domains = ["example.sales-snap.com", "alias.sales-snap.com"]
        captured: dict[str, list[str]] = {}

        def fake_certbot(args, *, timeout_sec):
            captured["args"] = list(args)
            return 0, "ok"

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "public_html"
            target.mkdir()
            instance_migrate._write_migration_target_marker(
                target,
                domains=domains,
                instance_uid=uid,
                instance_code=code,
            )
            instance_migrate._mark_migration_target_prepared(target)
            with (
                patch.object(instance_migrate, "identity_readback", return_value={"status": "ok"}),
                patch.object(instance_migrate, "_detect_php_version", return_value="8.3"),
                patch.object(instance_migrate, "_run_certbot", side_effect=fake_certbot),
                patch.object(
                    instance_migrate,
                    "_load_pem_certificate_identity",
                    return_value={
                        "status": "ok",
                        "dns_names": domains,
                        "not_after": "2027-02-19T00:00:00+00:00",
                    },
                ),
                patch.object(instance_migrate, "_verify_image_https", return_value={"status": "ok", "tls_verified": True}),
                patch.object(instance_migrate, "InstanceInventory", FakeInventory),
            ):
                result = instance_migrate.issue_target_certificate(
                    type("Cfg", (), {"state_db_path": str(Path(td) / "state.sqlite")})(),
                    target_root=str(target),
                    domains_json='["example.sales-snap.com","alias.sales-snap.com"]',
                    instance_uid=uid,
                    instance_code=code,
                    php_version="8.3",
                )

            marker_exists = instance_migrate._migration_target_marker(target).exists()

        self.assertTrue(result["ok"])
        self.assertFalse(result["migration_complete"])
        self.assertEqual(result["phase"], "target_tls_verified")
        self.assertEqual(result["next_operation"], "source-finalize")
        self.assertFalse(result["rollback_required"])
        self.assertFalse(marker_exists)
        self.assertEqual(captured["args"].count("-d"), 2)
        self.assertIn("--cert-name", captured["args"])
        self.assertTrue(
            {
                "schema", "ok", "phase", "migration_complete", "target_root", "domains",
                "instance_uid", "instance_code", "identity", "source_certificate_used",
                "rollback_required", "rollback_contract", "target_ready", "tls_pending",
                "next_operation", "certificate", "verification", "instances", "completed_at_utc",
            }.issubset(result)
        )

    def test_target_certificate_failure_requires_dns_and_source_rollback(self) -> None:
        uid = "a1179058-985d-4117-91b8-e951fa734a66"
        code = "00007"
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "public_html"
            target.mkdir()
            instance_migrate._write_migration_target_marker(
                target,
                domains=["example.sales-snap.com"],
                instance_uid=uid,
                instance_code=code,
            )
            instance_migrate._mark_migration_target_prepared(target)
            with (
                patch.object(instance_migrate, "identity_readback", return_value={"status": "ok"}),
                patch.object(instance_migrate, "_detect_php_version", return_value="8.3"),
                patch.object(instance_migrate, "_run_certbot", return_value=(1, "challenge failed")),
            ):
                result = instance_migrate.issue_target_certificate(
                    type("Cfg", (), {"state_db_path": str(Path(td) / "state.sqlite")})(),
                    target_root=str(target),
                    domains_json='["example.sales-snap.com"]',
                    instance_uid=uid,
                    instance_code=code,
                )
            marker = instance_migrate._read_migration_target_marker(target)

        self.assertFalse(result["ok"])
        self.assertFalse(result["migration_complete"])
        self.assertTrue(result["rollback_required"])
        self.assertEqual(result["phase"], "certificate_failed")
        self.assertEqual(result["error_code"], "certbot_failed")
        self.assertEqual(result["rollback_contract"]["dns"], "restore_source_route")
        self.assertEqual(marker["state"], "failed")
        self.assertTrue(
            {
                "schema", "ok", "phase", "migration_complete", "target_root", "domains",
                "instance_uid", "instance_code", "identity", "source_certificate_used",
                "rollback_required", "rollback_contract", "error_code", "error", "failed_at_utc",
            }.issubset(result)
        )

    def test_target_certificate_preflight_failure_is_a_strict_json_receipt(self) -> None:
        result = instance_migrate.issue_target_certificate(
            type("Cfg", (), {"state_db_path": "/tmp/state.sqlite"})(),
            target_root="/var/www/missing/public_html",
            domains_json='["example.sales-snap.com"]',
            instance_uid="a1179058-985d-4117-91b8-e951fa734a66",
            instance_code="00007",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["phase"], "certificate_failed")
        self.assertEqual(result["error_code"], "preflight_failed")
        self.assertTrue(result["rollback_required"])
        json.dumps(result)

    def test_source_operation_preflight_failures_are_strict_json_receipts(self) -> None:
        uid = "a1179058-985d-4117-91b8-e951fa734a66"
        code = "00007"

        class EmptyInventory:
            def __init__(self, _path: str) -> None:
                pass

            def list_instances(self):
                return []

        with tempfile.TemporaryDirectory() as td, patch.object(
            instance_migrate, "MIGRATION_SOURCE_STATE_ROOT", Path(td) / "source-state"
        ), patch.object(instance_migrate, "InstanceInventory", EmptyInventory), patch.object(
            instance_migrate, "ensure_seeded"
        ):
            cfg = type(
                "Cfg",
                (),
                {
                    "state_db_path": str(Path(td) / "state.sqlite"),
                    "scheduler_pause_flag_path": str(Path(td) / "scheduler.pause"),
                },
            )()
            quiesce = instance_migrate.quiesce_migration_source(
                cfg,
                selector="/var/www/missing/public_html",
                domains_json='["example.sales-snap.com"]',
                instance_uid=uid,
                instance_code=code,
            )
            restore = instance_migrate.restore_migration_source(
                cfg,
                selector="/var/www/missing/public_html",
                domains_json='["example.sales-snap.com"]',
                instance_uid=uid,
                instance_code=code,
            )
            finalize = instance_migrate.finalize_migration_source(
                cfg,
                selector="/var/www/missing/public_html",
                domains_json='["example.sales-snap.com"]',
                instance_uid=uid,
                instance_code=code,
            )

        expected = {
            "source-quiesce": (
                quiesce,
                {
                    "schema", "ok", "phase", "source_quiescent", "source_authoritative",
                    "source_root", "domains", "instance_uid", "instance_code",
                    "identity_adoption",
                    "maintenance_vhost_active", "maintenance_vhost_static_503", "task_drain",
                    "web_request_drain", "preservation", "error", "restore_errors", "rollback_required",
                },
            ),
            "source-restore": (
                restore,
                {
                    "schema", "ok", "phase", "source_authoritative", "source_quiescent",
                    "original_vhost_active", "source_root", "domains", "instance_uid",
                    "instance_code", "identity_adoption", "preservation", "runtime_restore", "error",
                    "rollback_required", "retryable",
                },
            ),
            "source-finalize": (
                finalize,
                {
                    "schema", "ok", "phase", "migration_complete", "source_authoritative",
                    "source_quiescent", "recoverable_artifact", "maintenance_vhost_active",
                    "source_root", "domains", "instance_uid", "instance_code", "artifact_state",
                    "identity_adoption", "preservation", "runtime_restore", "error", "rollback_required", "retryable",
                },
            ),
        }
        for operation, (receipt, fields) in expected.items():
            self.assertFalse(receipt["ok"], operation)
            self.assertTrue(fields.issubset(receipt), operation)
            json.dumps(receipt)

    def _source_migration_fixture(self, td: str):
        uid = "a1179058-985d-4117-91b8-e951fa734a66"
        code = "00007"
        domain = "example.sales-snap.com"
        base = Path(td)
        root = base / "source" / "public_html"
        root.mkdir(parents=True)
        (root / "index.php").write_text("<?php\n", encoding="utf-8")
        instance_migrate.write_instance_identity(
            root,
            instance_uid=uid,
            instance_code=code,
            primary_domain=domain,
        )
        available = base / "nginx" / "sites-available"
        enabled = base / "nginx" / "sites-enabled"
        available.mkdir(parents=True)
        enabled.mkdir(parents=True)
        original = available / "example.conf"
        original_text = (
            "server {\n"
            "    listen 80;\n"
            "    listen 443 ssl;\n"
            f"    server_name {domain};\n"
            f"    root {root};\n"
            "    ssl_certificate /etc/letsencrypt/live/example/fullchain.pem;\n"
            "    ssl_certificate_key /etc/letsencrypt/live/example/privkey.pem;\n"
            "    location ~ \\.php$ { fastcgi_pass unix:/run/php/php8.3-fpm.sock; }\n"
            "}\n"
        )
        original.write_text(original_text, encoding="utf-8")
        active = enabled / "example.conf"
        active.symlink_to(original)
        install = MauticInstall(
            instance_uid=uid,
            instance_code=code,
            name=domain,
            root=str(root),
            console_path=str(root / "bin" / "console"),
            primary_domain=domain,
            domains=[domain],
        )
        cfg = type(
            "Cfg",
            (),
            {
                "state_db_path": str(base / "state.sqlite"),
                "scheduler_pause_flag_path": str(base / "mcd" / "scheduler.pause"),
            },
        )()
        return uid, code, domain, root, original, original_text, active, available, enabled, install, cfg

    def test_source_quiesce_materializes_missing_legacy_marker_from_exact_mcc_identity(self) -> None:
        class FakeInventory:
            installs = []

            def __init__(self, _path: str) -> None:
                pass

            def list_instances(self):
                return list(self.installs)

        with tempfile.TemporaryDirectory() as td:
            uid, code, domain, root, original, _original_text, active, available, enabled, install, cfg = self._source_migration_fixture(td)
            marker = root / instance_migrate.INSTANCE_IDENTITY_MARKER
            marker.unlink()
            install.instance_uid = "legacy-domain-derived-uid"
            install.instance_code = None
            FakeInventory.installs = [install]
            state_root = Path(td) / "mcd" / "source-state"
            with (
                patch.object(instance_migrate, "NGINX_SITES_AVAILABLE", available),
                patch.object(instance_migrate, "NGINX_SITES_ENABLED", enabled),
                patch.object(instance_migrate, "MIGRATION_SOURCE_STATE_ROOT", state_root),
                patch.object(instance_migrate, "MIGRATION_SOURCE_MAINTENANCE_ROOT", Path(td) / "mcd" / "maintenance"),
                patch.object(instance_migrate, "InstanceInventory", FakeInventory),
                patch.object(instance_migrate, "ensure_seeded"),
                patch.object(instance_migrate, "collect_maintenance_state", return_value={"cron_marker_present": False}),
                patch.object(instance_migrate, "stop_cron_service", return_value={"ok": True}),
                patch.object(instance_migrate, "stop_running_tasks_for_maintenance", return_value={"ok": True}),
                patch.object(instance_migrate, "_drain_reloaded_nginx_workers", return_value={"ok": True}),
                patch.object(instance_migrate, "restore_cron_service_if_needed", return_value={"ok": True}),
                patch.object(instance_migrate, "_run_checked", return_value=""),
            ):
                result = instance_migrate.quiesce_migration_source(
                    cfg,
                    selector=str(root),
                    domains_json=f'["{domain}"]',
                    instance_uid=uid,
                    instance_code=code,
                )
                maintenance_target = active.readlink()
                restored = instance_migrate.restore_migration_source(
                    cfg,
                    selector=str(root),
                    domains_json=f'["{domain}"]',
                    instance_uid=uid,
                    instance_code=code,
                )
                restored_again = instance_migrate.restore_migration_source(
                    cfg,
                    selector=str(root),
                    domains_json=f'["{domain}"]',
                    instance_uid=uid,
                    instance_code=code,
                )

            observed = instance_migrate.read_instance_identity(root)
            persisted = json.loads((state_root / f"{uid}.json").read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertTrue(result["identity_adoption"]["materialized_from_mcc"])
            self.assertEqual(result["identity_adoption"]["reason"], "legacy_source_marker_missing")
            self.assertEqual(result["identity_adoption"]["inventory_identity"]["instance_uid"], "legacy-domain-derived-uid")
            self.assertNotEqual(maintenance_target, original)
            self.assertEqual(result["preservation"]["original_symlink_target"], str(original))
            self.assertEqual(persisted["original_symlink_target"], str(original))
            self.assertTrue(restored["ok"])
            self.assertEqual(restored["preservation"]["original_symlink_target"], str(original))
            self.assertTrue(restored_again["ok"])
            self.assertEqual(restored_again["preservation"]["original_symlink_target"], str(original))
            self.assertEqual(active.readlink(), original)
            self.assertEqual(observed["instance_uid"], uid)
            self.assertEqual(observed["instance_code"], code)
            self.assertEqual(observed["primary_domain"], domain)

    def test_source_quiesce_never_overwrites_invalid_existing_identity_marker(self) -> None:
        class FakeInventory:
            installs = []

            def __init__(self, _path: str) -> None:
                pass

            def list_instances(self):
                return list(self.installs)

        with tempfile.TemporaryDirectory() as td:
            uid, code, domain, root, _original, _original_text, _active, _available, _enabled, install, cfg = self._source_migration_fixture(td)
            marker = root / instance_migrate.INSTANCE_IDENTITY_MARKER
            marker.write_text("{invalid\n", encoding="utf-8")
            before = marker.read_bytes()
            FakeInventory.installs = [install]
            with (
                patch.object(instance_migrate, "MIGRATION_SOURCE_STATE_ROOT", Path(td) / "mcd" / "source-state"),
                patch.object(instance_migrate, "InstanceInventory", FakeInventory),
                patch.object(instance_migrate, "ensure_seeded"),
            ):
                result = instance_migrate.quiesce_migration_source(
                    cfg,
                    selector=str(root),
                    domains_json=f'["{domain}"]',
                    instance_uid=uid,
                    instance_code=code,
                )

            self.assertFalse(result["ok"])
            self.assertIn("present but invalid", result["error"])
            self.assertEqual(marker.read_bytes(), before)
            self.assertEqual(result["identity_adoption"], {})

    def test_legacy_identity_adoption_requires_exact_root_and_inventory_domain(self) -> None:
        class FakeInventory:
            installs = []

            def __init__(self, _path: str) -> None:
                pass

            def list_instances(self):
                return list(self.installs)

        with tempfile.TemporaryDirectory() as td:
            uid, code, domain, root, _original, _original_text, _active, _available, _enabled, install, cfg = self._source_migration_fixture(td)
            marker = root / instance_migrate.INSTANCE_IDENTITY_MARKER
            marker.unlink()
            install.instance_uid = "legacy-domain-derived-uid"
            install.instance_code = None
            FakeInventory.installs = [install]
            with (
                patch.object(instance_migrate, "MIGRATION_SOURCE_STATE_ROOT", Path(td) / "mcd" / "source-state"),
                patch.object(instance_migrate, "InstanceInventory", FakeInventory),
                patch.object(instance_migrate, "ensure_seeded"),
            ):
                non_root = instance_migrate.quiesce_migration_source(
                    cfg,
                    selector=domain,
                    domains_json=f'["{domain}"]',
                    instance_uid=uid,
                    instance_code=code,
                )
                wrong_domain = instance_migrate.quiesce_migration_source(
                    cfg,
                    selector=str(root),
                    domains_json='["other.sales-snap.com"]',
                    instance_uid=uid,
                    instance_code=code,
                )

            self.assertFalse(non_root["ok"])
            self.assertIn("exact absolute source root", non_root["error"])
            self.assertFalse(wrong_domain["ok"])
            self.assertIn("inventory primary domain", wrong_domain["error"])
            self.assertFalse(marker.exists())

    def test_source_quiesce_uses_static_503_and_rollback_restores_original_symlink(self) -> None:
        class FakeInventory:
            installs = []

            def __init__(self, _path: str) -> None:
                pass

            def list_instances(self):
                return list(self.installs)

            def rescan(self, _config):
                return list(self.installs)

        with tempfile.TemporaryDirectory() as td:
            uid, code, domain, root, original, original_text, active, available, enabled, install, cfg = self._source_migration_fixture(td)
            FakeInventory.installs = [install]
            state_root = Path(td) / "mcd" / "source-state"
            maintenance_root = Path(td) / "mcd" / "maintenance"
            atomic_symlink_replace = instance_migrate._atomic_symlink_replace
            snapshot_before_switch = {}

            def observed_atomic_symlink_replace(link: Path, target: str) -> None:
                if not snapshot_before_switch:
                    state_path = state_root / f"{uid}.json"
                    snapshot_before_switch.update(json.loads(state_path.read_text(encoding="utf-8")))
                atomic_symlink_replace(link, target)

            with (
                patch.object(instance_migrate, "NGINX_SITES_AVAILABLE", available),
                patch.object(instance_migrate, "NGINX_SITES_ENABLED", enabled),
                patch.object(instance_migrate, "MIGRATION_SOURCE_STATE_ROOT", state_root),
                patch.object(instance_migrate, "MIGRATION_SOURCE_MAINTENANCE_ROOT", maintenance_root),
                patch.object(instance_migrate, "InstanceInventory", FakeInventory),
                patch.object(instance_migrate, "ensure_seeded"),
                patch.object(instance_migrate, "collect_maintenance_state", return_value={"cron_marker_present": False}),
                patch.object(instance_migrate, "stop_cron_service", return_value={"ok": True, "was_active": True}),
                patch.object(
                    instance_migrate,
                    "stop_running_tasks_for_maintenance",
                    return_value={"ok": True, "stable_snapshots": 3, "tracked_running": 0},
                ),
                patch.object(
                    instance_migrate,
                    "_drain_reloaded_nginx_workers",
                    return_value={"ok": True, "stable_snapshots": 3, "remaining_worker_pids": []},
                ),
                patch.object(instance_migrate, "restore_cron_service_if_needed", return_value={"ok": True, "started": True}),
                patch.object(instance_migrate, "_atomic_symlink_replace", side_effect=observed_atomic_symlink_replace),
                patch.object(instance_migrate, "_run_checked", return_value=""),
            ):
                quiesced = instance_migrate.quiesce_migration_source(
                    cfg,
                    selector=str(root),
                    domains_json=f'["{domain}"]',
                    instance_uid=uid,
                    instance_code=code,
                )
                maintenance_config = Path(quiesced["preservation"]["original_config"]).parent / f"zz-mcd-migration-maintenance-{code}.conf"
                active_target_during_quiescence = (active.parent / active.readlink()).resolve()
                maintenance_text = maintenance_config.read_text(encoding="utf-8")
                restored = instance_migrate.restore_migration_source(
                    cfg,
                    selector=str(root),
                    domains_json=f'["{domain}"]',
                    instance_uid=uid,
                    instance_code=code,
                )

            self.assertTrue(quiesced["ok"])
            self.assertTrue(quiesced["source_quiescent"])
            self.assertTrue(
                {
                    "schema", "ok", "phase", "source_quiescent", "source_authoritative",
                    "source_root", "domains", "instance_uid", "instance_code",
                    "identity_adoption",
                    "maintenance_vhost_active", "maintenance_vhost_static_503", "task_drain",
                    "web_request_drain", "preservation",
                }.issubset(quiesced)
            )
            self.assertIn("return 503", maintenance_text)
            self.assertIn("listen 443 ssl", maintenance_text)
            self.assertIn("/etc/letsencrypt/live/example/fullchain.pem", maintenance_text)
            self.assertNotIn("fastcgi", maintenance_text.lower())
            self.assertNotIn("proxy_pass", maintenance_text.lower())
            self.assertEqual(active_target_during_quiescence, maintenance_config.resolve())
            self.assertEqual(snapshot_before_switch["original_symlink_target"], str(original))
            self.assertNotIn("original_link_target", snapshot_before_switch)
            self.assertEqual(quiesced["preservation"]["original_symlink_target"], str(original))
            self.assertNotIn("original_link_target", quiesced["preservation"])
            self.assertEqual(original.read_text(encoding="utf-8"), original_text)
            self.assertTrue(restored["ok"])
            self.assertTrue(
                {
                    "schema", "ok", "phase", "source_authoritative", "source_quiescent",
                    "original_vhost_active", "source_root", "domains", "instance_uid",
                    "instance_code", "identity_adoption", "preservation", "runtime_restore",
                }.issubset(restored)
            )
            self.assertEqual(active.readlink(), original)
            self.assertEqual(restored["preservation"]["original_symlink_target"], str(original))
            self.assertTrue(restored["preservation"]["original_config_unchanged"])

    def test_source_finalize_keeps_files_and_vhost_as_recoverable_artifact(self) -> None:
        class FakeInventory:
            installs = []

            def __init__(self, _path: str) -> None:
                pass

            def list_instances(self):
                return list(self.installs)

            def rescan(self, _config):
                return []

        with tempfile.TemporaryDirectory() as td:
            uid, code, domain, root, original, original_text, active, available, enabled, install, cfg = self._source_migration_fixture(td)
            FakeInventory.installs = [install]
            state_root = Path(td) / "mcd" / "source-state"
            maintenance_root = Path(td) / "mcd" / "maintenance"
            with (
                patch.object(instance_migrate, "NGINX_SITES_AVAILABLE", available),
                patch.object(instance_migrate, "NGINX_SITES_ENABLED", enabled),
                patch.object(instance_migrate, "MIGRATION_SOURCE_STATE_ROOT", state_root),
                patch.object(instance_migrate, "MIGRATION_SOURCE_MAINTENANCE_ROOT", maintenance_root),
                patch.object(instance_migrate, "InstanceInventory", FakeInventory),
                patch.object(instance_migrate, "ensure_seeded"),
                patch.object(instance_migrate, "collect_maintenance_state", return_value={"cron_marker_present": False}),
                patch.object(instance_migrate, "stop_cron_service", return_value={"ok": True}),
                patch.object(instance_migrate, "stop_running_tasks_for_maintenance", return_value={"ok": True}),
                patch.object(instance_migrate, "_drain_reloaded_nginx_workers", return_value={"ok": True}),
                patch.object(instance_migrate, "restore_cron_service_if_needed", return_value={"ok": True}),
                patch.object(instance_migrate, "_run_checked", return_value=""),
            ):
                quiesced = instance_migrate.quiesce_migration_source(
                    cfg,
                    selector=str(root),
                    domains_json=f'["{domain}"]',
                    instance_uid=uid,
                    instance_code=code,
                )
                maintenance_target = (active.parent / active.readlink()).resolve()
                finalized = instance_migrate.finalize_migration_source(
                    cfg,
                    selector=str(root),
                    domains_json=f'["{domain}"]',
                    instance_uid=uid,
                    instance_code=code,
                )

            state = json.loads((root / ".mcd" / "state" / "instance-state.json").read_text(encoding="utf-8"))
            self.assertTrue(quiesced["ok"])
            self.assertTrue(finalized["ok"])
            self.assertTrue(finalized["migration_complete"])
            self.assertTrue(
                {
                    "schema", "ok", "phase", "migration_complete", "source_authoritative",
                    "source_quiescent", "recoverable_artifact", "maintenance_vhost_active",
                    "source_root", "domains", "instance_uid", "instance_code", "artifact_state",
                    "identity_adoption", "preservation", "runtime_restore",
                }.issubset(finalized)
            )
            self.assertFalse(finalized["source_authoritative"])
            self.assertTrue(finalized["recoverable_artifact"])
            self.assertEqual(finalized["preservation"]["original_symlink_target"], str(original))
            self.assertEqual(state["profile"], "artifact")
            self.assertTrue(root.is_dir())
            self.assertEqual(original.read_text(encoding="utf-8"), original_text)
            self.assertEqual((active.parent / active.readlink()).resolve(), maintenance_target)

    def test_source_restore_rejects_legacy_or_conflicting_symlink_snapshot(self) -> None:
        class FakeInventory:
            installs = []

            def __init__(self, _path: str) -> None:
                pass

            def list_instances(self):
                return list(self.installs)

        for invalid_snapshot in ("legacy_alias_only", "conflicting_canonical"):
            with self.subTest(invalid_snapshot=invalid_snapshot), tempfile.TemporaryDirectory() as td:
                uid, code, domain, root, original, _original_text, active, available, enabled, install, cfg = self._source_migration_fixture(td)
                FakeInventory.installs = [install]
                state_root = Path(td) / "mcd" / "source-state"
                with (
                    patch.object(instance_migrate, "NGINX_SITES_AVAILABLE", available),
                    patch.object(instance_migrate, "NGINX_SITES_ENABLED", enabled),
                    patch.object(instance_migrate, "MIGRATION_SOURCE_STATE_ROOT", state_root),
                    patch.object(instance_migrate, "MIGRATION_SOURCE_MAINTENANCE_ROOT", Path(td) / "mcd" / "maintenance"),
                    patch.object(instance_migrate, "InstanceInventory", FakeInventory),
                    patch.object(instance_migrate, "ensure_seeded"),
                    patch.object(instance_migrate, "collect_maintenance_state", return_value={"cron_marker_present": False}),
                    patch.object(instance_migrate, "stop_cron_service", return_value={"ok": True}),
                    patch.object(instance_migrate, "stop_running_tasks_for_maintenance", return_value={"ok": True}),
                    patch.object(instance_migrate, "_drain_reloaded_nginx_workers", return_value={"ok": True}),
                    patch.object(instance_migrate, "restore_cron_service_if_needed", return_value={"ok": True}),
                    patch.object(instance_migrate, "_run_checked", return_value=""),
                ):
                    quiesced = instance_migrate.quiesce_migration_source(
                        cfg,
                        selector=str(root),
                        domains_json=f'["{domain}"]',
                        instance_uid=uid,
                        instance_code=code,
                    )
                    maintenance_target = active.readlink()
                    state_path = state_root / f"{uid}.json"
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    self.assertNotIn("original_link_target", state)
                    if invalid_snapshot == "legacy_alias_only":
                        state.pop("original_symlink_target")
                        state["original_link_target"] = str(original)
                    else:
                        state["original_symlink_target"] = str(available / "wrong.conf")
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    restored = instance_migrate.restore_migration_source(
                        cfg,
                        selector=str(root),
                        domains_json=f'["{domain}"]',
                        instance_uid=uid,
                        instance_code=code,
                    )

                self.assertTrue(quiesced["ok"])
                self.assertFalse(restored["ok"])
                self.assertEqual(restored["phase"], "source_restore_failed")
                self.assertTrue(restored["rollback_required"])
                self.assertEqual(active.readlink(), maintenance_target)

    def test_source_quiesce_rejects_broken_original_symlink(self) -> None:
        class FakeInventory:
            installs = []

            def __init__(self, _path: str) -> None:
                pass

            def list_instances(self):
                return list(self.installs)

        with tempfile.TemporaryDirectory() as td:
            uid, code, domain, root, original, _original_text, active, available, enabled, install, cfg = self._source_migration_fixture(td)
            FakeInventory.installs = [install]
            original.unlink()
            with (
                patch.object(instance_migrate, "NGINX_SITES_AVAILABLE", available),
                patch.object(instance_migrate, "NGINX_SITES_ENABLED", enabled),
                patch.object(instance_migrate, "MIGRATION_SOURCE_STATE_ROOT", Path(td) / "mcd" / "source-state"),
                patch.object(instance_migrate, "InstanceInventory", FakeInventory),
                patch.object(instance_migrate, "ensure_seeded"),
            ):
                result = instance_migrate.quiesce_migration_source(
                    cfg,
                    selector=str(root),
                    domains_json=f'["{domain}"]',
                    instance_uid=uid,
                    instance_code=code,
                )

            self.assertFalse(result["ok"])
            self.assertIn("active source nginx symlink was not found", result["error"])
            self.assertTrue(active.is_symlink())
            self.assertFalse(active.exists())

    def test_source_quiesce_failure_restores_original_vhost_and_runtime(self) -> None:
        class FakeInventory:
            installs = []

            def __init__(self, _path: str) -> None:
                pass

            def list_instances(self):
                return list(self.installs)

        with tempfile.TemporaryDirectory() as td:
            uid, code, domain, root, original, original_text, active, available, enabled, install, cfg = self._source_migration_fixture(td)
            FakeInventory.installs = [install]
            with (
                patch.object(instance_migrate, "NGINX_SITES_AVAILABLE", available),
                patch.object(instance_migrate, "NGINX_SITES_ENABLED", enabled),
                patch.object(instance_migrate, "MIGRATION_SOURCE_STATE_ROOT", Path(td) / "mcd" / "source-state"),
                patch.object(instance_migrate, "MIGRATION_SOURCE_MAINTENANCE_ROOT", Path(td) / "mcd" / "maintenance"),
                patch.object(instance_migrate, "InstanceInventory", FakeInventory),
                patch.object(instance_migrate, "ensure_seeded"),
                patch.object(instance_migrate, "collect_maintenance_state", return_value={"cron_marker_present": False}),
                patch.object(instance_migrate, "stop_cron_service", return_value={"ok": True}),
                patch.object(
                    instance_migrate,
                    "stop_running_tasks_for_maintenance",
                    return_value={"ok": False, "tracked_running": 1, "drain_timed_out": True},
                ),
                patch.object(instance_migrate, "restore_cron_service_if_needed", return_value={"ok": True}),
                patch.object(instance_migrate, "_run_checked", return_value=""),
            ):
                result = instance_migrate.quiesce_migration_source(
                    cfg,
                    selector=str(root),
                    domains_json=f'["{domain}"]',
                    instance_uid=uid,
                    instance_code=code,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["phase"], "source_quiesce_failed")
            self.assertFalse(result["rollback_required"])
            self.assertEqual(active.readlink(), original)
            self.assertEqual(original.read_text(encoding="utf-8"), original_text)
            self.assertFalse(Path(cfg.scheduler_pause_flag_path).exists())

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
            if "source-restore" in args:
                events.append("source_restore")
                return json.dumps({"ok": True, "phase": "source_restored"})
            events.append("source_quiesce")
            return json.dumps({"ok": True, "phase": "quiescent", "source_quiescent": True})

        def fake_tunnel(**_kwargs):
            events.append("tunnel")
            return FakeProc(), 3307

        def fake_dump(_config, *, local_source_db, target_db, label: str):
            events.append(f"db_import:{label}")
            return {"label": label}

        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / "source.key"
            key.write_text("key", encoding="utf-8")
            migration_root = (Path(td) / "www").resolve()
            target_root = migration_root / "example.com_00007" / "public_html"
            cfg = type("Cfg", (), {"state_db_path": str(Path(td) / "state.sqlite")})()

            with (
                patch.object(instance_migrate, "MIGRATION_ROOT", migration_root),
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
                    instance_uid="a1179058-985d-4117-91b8-e951fa734a66",
                    instance_code="00007",
                )

        self.assertTrue(result["ok"])
        self.assertTrue(result["source_maintenance_active"])
        self.assertFalse(result["source_maintenance_restored"])
        self.assertTrue(result["source_quiescent"])
        self.assertEqual([x for x in events if x.startswith("db_import:")], ["db_import:single"])
        self.assertLess(events.index("source_quiesce"), events.index("db_import:single"))
        self.assertLess(events.index("db_import:single"), events.index("patch_db"))
        self.assertNotIn("source_restore", events)
        self.assertFalse(result["migration_complete"])
        self.assertTrue(result["dns_cutover_ready"])
        self.assertTrue(result["tls_pending"])
        self.assertFalse(result["source_certificate_used"])
        self.assertFalse(result["source_instance_state_changed"])
        self.assertEqual(result["next_operation"], "target-issue-certificate")
        self.assertNotIn("source_state_artifact", events)

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
            patch.object(instance_migrate, "_dump_supports_network_timeout", return_value=True),
        ):
            output = io.BytesIO()
            instance_migrate.stream_source_db(cfg, selector="/var/www/example", output=output)

        argv = captured["argv"]
        self.assertEqual(argv[0], "/usr/bin/mariadb-dump")
        self.assertIn("--routines", argv)
        self.assertIn("--triggers", argv)
        self.assertIn("--max-allowed-packet=1073741824", argv)
        self.assertIn("--network-timeout", argv)
        self.assertNotIn("--events", argv)

    def test_source_db_stream_emits_complete_large_table_only_after_dump_success(self) -> None:
        class FakeInventory:
            def __init__(self, _path: str) -> None:
                pass

            def list_instances(self):
                return [
                    MauticInstall(
                        instance_uid="uid-large",
                        name="large",
                        root="/var/www/large",
                        console_path="/var/www/large/bin/console",
                    )
                ]

        audit_rows = b"INSERT INTO `ss_audit_log` VALUES (1,'payload');\n" * 40000

        class FakeProc:
            def __init__(self) -> None:
                self.stdout = io.BytesIO(audit_rows)

            def wait(self):
                return 0

        cfg = type("Cfg", (), {"state_db_path": "/tmp/mcd-state.sqlite"})()
        db = DBConfig(
            host="localhost",
            port=3306,
            name="baza_large",
            user="source_user",
            password="source_password",
            table_prefix="ss_",
        )

        with (
            patch.object(instance_migrate, "InstanceInventory", FakeInventory),
            patch.object(instance_migrate, "ensure_seeded", return_value=None),
            patch.object(instance_migrate, "_db_from_local_php", return_value=db),
            patch.object(instance_migrate.shutil, "which", return_value="/usr/bin/mariadb-dump"),
            patch.object(instance_migrate, "_dump_supports_network_timeout", return_value=False),
            patch.object(instance_migrate.subprocess, "Popen", return_value=FakeProc()),
            patch.object(instance_migrate.secrets, "token_hex", return_value="b" * 32),
        ):
            output = io.BytesIO()
            result = instance_migrate.stream_source_db(cfg, selector="/var/www/large", output=output)

        with gzip.GzipFile(fileobj=io.BytesIO(output.getvalue()), mode="rb") as gz:
            emitted = gz.read()
        self.assertEqual(result, 0)
        self.assertTrue(emitted.startswith(instance_migrate._db_stream_marker("BEGIN", "b" * 32)))
        self.assertIn(audit_rows, emitted)
        self.assertTrue(emitted.rstrip().endswith(instance_migrate._db_stream_marker("COMPLETE", "b" * 32).rstrip()))

    def test_source_db_stream_propagates_dump_failure_without_emitting_partial_output(self) -> None:
        class FakeInventory:
            def __init__(self, _path: str) -> None:
                pass

            def list_instances(self):
                return [
                    MauticInstall(
                        instance_uid="uid-failed",
                        name="failed",
                        root="/var/www/failed",
                        console_path="/var/www/failed/bin/console",
                    )
                ]

        class FakeProc:
            def __init__(self) -> None:
                self.stdout = io.BytesIO(b"INSERT INTO `ss_audit_log` VALUES (353013);\n")

            def wait(self):
                return 3

        def fake_popen(_argv, **kwargs):
            kwargs["stderr"].write(
                b"mariadb-dump: Error 2013: Lost connection to server during query "
                b"when dumping table ss_audit_log at row: 353013\n"
            )
            return FakeProc()

        cfg = type("Cfg", (), {"state_db_path": "/tmp/mcd-state.sqlite"})()
        db = DBConfig(
            host="localhost",
            port=3306,
            name="baza_failed",
            user="source_user",
            password="source_password",
            table_prefix="ss_",
        )
        output = io.BytesIO()
        with (
            patch.object(instance_migrate, "InstanceInventory", FakeInventory),
            patch.object(instance_migrate, "ensure_seeded", return_value=None),
            patch.object(instance_migrate, "_db_from_local_php", return_value=db),
            patch.object(instance_migrate.shutil, "which", return_value="/usr/bin/mariadb-dump"),
            patch.object(instance_migrate, "_dump_supports_network_timeout", return_value=False),
            patch.object(instance_migrate.subprocess, "Popen", side_effect=fake_popen),
        ):
            with self.assertRaisesRegex(RuntimeError, r"rc=3.*Lost connection.*ss_audit_log"):
                instance_migrate.stream_source_db(cfg, selector="/var/www/failed", output=output)

        self.assertEqual(output.getvalue(), b"")

    def test_target_db_rejects_truncated_source_before_preparing_database(self) -> None:
        truncated = io.BytesIO()
        with gzip.GzipFile(fileobj=truncated, mode="wb") as gz:
            gz.write(instance_migrate._db_stream_marker("BEGIN", "c" * 32))
            gz.write(b"CREATE TABLE `ss_audit_log` (`id` int);\n")
            gz.write(b"INSERT INTO `ss_audit_log` VALUES (1);\n")
        truncated.seek(0)

        with (
            patch.object(instance_migrate, "_prepare_target_db") as prepare,
            patch.object(instance_migrate.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(RuntimeError, "completion marker is missing"):
                instance_migrate.import_target_db_stream(
                    target_db_name="baza_target",
                    target_db_user="korisnik_target",
                    target_db_password="target_password",
                    input_stream=truncated,
                )

        prepare.assert_not_called()
        popen.assert_not_called()

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
        gzipped = _complete_db_stream(sql.encode("utf-8"))

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

    def test_target_db_stream_omits_any_generated_column_from_import_rows(self) -> None:
        sql = (
            "CREATE TABLE `ss_email_stats` (\n"
            "  `id` int NOT NULL,\n"
            "  `date_sent` datetime NOT NULL,\n"
            "  `generated_sent_date` date GENERATED ALWAYS AS (date(`date_sent`)) VIRTUAL,\n"
            "  PRIMARY KEY (`id`)\n"
            ") ENGINE=InnoDB;\n"
            "CREATE TABLE `custom_derived_rows` (\n"
            "  `id` int NOT NULL,\n"
            "  `base_value` int NOT NULL,\n"
            "  `derived_value` int GENERATED ALWAYS AS (`base_value` * 2) STORED,\n"
            "  PRIMARY KEY (`id`)\n"
            ") ENGINE=InnoDB;\n"
            "INSERT INTO `ss_email_stats` VALUES\n"
            "(1,'2026-05-27 10:00:00','2026-05-27');\n"
            "INSERT INTO `custom_derived_rows` VALUES\n"
            "(1,21,42);\n"
        )
        gzipped = _complete_db_stream(sql.encode("utf-8"))

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

        with (
            patch.object(instance_migrate, "_prepare_target_db", return_value=None),
            patch.object(instance_migrate, "_mysql_admin_base", return_value=["mysql"]),
            patch.object(instance_migrate.subprocess, "Popen", return_value=FakeProc()),
        ):
            result = instance_migrate.import_target_db_stream(
                target_db_name="baza_target",
                target_db_user="korisnik_target",
                target_db_password="target_password",
                input_stream=gzipped,
            )

        imported_sql = fake_stdin.getvalue().decode("utf-8")
        self.assertTrue(result["ok"])
        self.assertIn(
            "INSERT INTO `ss_email_stats` (`id`,`date_sent`) VALUES\n"
            "(1,'2026-05-27 10:00:00');",
            imported_sql,
        )
        self.assertIn(
            "INSERT INTO `custom_derived_rows` (`id`,`base_value`) VALUES\n"
            "(1,21);",
            imported_sql,
        )
        self.assertNotIn("'2026-05-27');", imported_sql)
        self.assertNotIn("(1,21,42);", imported_sql)


if __name__ == "__main__":
    unittest.main()
