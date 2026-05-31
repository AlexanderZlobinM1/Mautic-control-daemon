from __future__ import annotations

import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

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

    def test_safe_tar_member_target_rejects_traversal(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsafe tar member path"):
            instance_migrate._safe_tar_member_target(Path("/"), "../etc/passwd")

    def test_nginx_web_root_prefers_mautic_docroot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "docroot").mkdir()
            (root / "docroot" / "index.php").write_text("<?php\n", encoding="utf-8")

            self.assertEqual(instance_migrate._nginx_web_root(root), root / "docroot")


if __name__ == "__main__":
    unittest.main()
