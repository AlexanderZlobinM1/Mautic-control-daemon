from __future__ import annotations

import tempfile
import tarfile
import unittest
import inspect
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

from mcd_agent import mautic_image_install as image_install
from mcd_agent.mautic_image_install import _safe_extract


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

    def test_failed_install_rollback_requests_only_owned_database_resources(self) -> None:
        plan = image_install.build_plan(
            image_ref="default7",
            domain="shop.sales-snap.com",
            php_version="8.4",
        )
        calls: list[tuple[str, str, dict[str, str]]] = []

        def external(_cfg, *, component: str, operation: str, operation_input: dict[str, str]):
            calls.append((component, operation, operation_input))
            return {"status": "ok"}

        with patch.object(image_install, "run_upgrade_os_operation", side_effect=external):
            errors = image_install._rollback_failed_install(
                SimpleNamespace(),
                plan,
                operation_id="image-1",
                database_owned=True,
                certificate_owned=False,
                prepared_root=Path("/tmp/prepared-image-1"),
                tree_materialized=False,
                vhost_ownership_receipt=None,
                mail_requested=False,
            )

        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        component, operation, operation_input = calls[0]
        self.assertEqual((component, operation), ("database", "database_prepare_import"))
        self.assertEqual(operation_input["action"], "rollback_owned")
        self.assertEqual(operation_input["operation_id"], "image-1")

    def test_failed_install_rollback_uses_owned_vhost_and_tree_contracts(self) -> None:
        plan = image_install.build_plan(
            image_ref="default7", domain="shop.sales-snap.com", php_version="8.4"
        )
        calls: list[tuple[str, str, dict[str, str]]] = []
        receipt = {"schema": "receipt"}

        def external(_cfg, *, component: str, operation: str, operation_input: dict[str, str]):
            calls.append((component, operation, operation_input))
            return {"status": "ok"}

        with (
            patch.object(image_install, "run_upgrade_os_operation", side_effect=external),
            patch.object(image_install, "_external_cleanup_managed_vhost", return_value={"status": "applied"}) as cleanup,
        ):
            errors = image_install._rollback_failed_install(
                SimpleNamespace(), plan, operation_id="image-1",
                database_owned=False, certificate_owned=False,
                prepared_root=Path("/tmp/prepared-image-1"),
                tree_materialized=True, vhost_ownership_receipt=receipt,
                mail_requested=False,
            )

        self.assertEqual(errors, [])
        cleanup.assert_called_once_with(
            operation_id="image-1", domain=plan.domain,
            ownership_receipt=receipt, cfg=ANY,
        )
        tree = next(call for call in calls if call[:2] == ("filesystem", "upgrade_tree_transition"))
        self.assertEqual(tree[2]["action"], "rollback_materialized")
        self.assertEqual(tree[2]["prepared_root"], "/tmp/prepared-image-1")

    def test_package_preflight_uses_external_image_install_role(self) -> None:
        cfg = SimpleNamespace()
        with patch.object(image_install, "run_upgrade_os_operation", return_value={"status": "ok"}) as dispatch:
            image_install._apt_install_role(cfg, "image_install")
        dispatch.assert_called_once_with(
            cfg,
            component="apt",
            operation="apt_apply",
            operation_input={
                "package_role": "image_install",
                "package_state": "present",
                "force_repo_rescan": "true",
                "dry_run": "false",
            },
        )

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

if __name__ == "__main__":
    unittest.main()
