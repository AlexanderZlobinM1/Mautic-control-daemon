from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcd_agent import cli
import mcd_agent.instance_delete as instance_delete
from mcd_agent.inventory import InstanceInventory


class _Inventory:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def list_instances(self) -> list[object]:
        return list(self._rows)


class InstanceDeleteCliTests(unittest.TestCase):
    def test_delete_drops_exact_managed_image_database_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "public_html"
            root.mkdir()
            plan = instance_delete.InstanceDeletePlan(
                root=root,
                local_php=None,
                domains=[],
                nginx_paths=[],
                db_name="baza_shop",
                db_host="localhost",
                db_port="3306",
                db_user="korisnik_shop",
                db_password="secret",
                delete_files=False,
                delete_vhost=False,
                delete_db=True,
            )
            sql: list[str] = []

            def mysql_exec(statement: str, **_kwargs: object) -> str:
                sql.append(statement)
                return ""

            with (
                patch.object(instance_delete, "build_delete_plan", return_value=plan),
                patch.object(instance_delete.os, "geteuid", return_value=0),
                patch.object(instance_delete, "_mysql_exec", side_effect=mysql_exec),
                patch("mcd_agent.inventory.InstanceInventory.rescan", return_value=0),
            ):
                result = instance_delete.delete_instance_artifacts(
                    SimpleNamespace(state_db_path=str(Path(tmp) / "state.db")),
                    root=str(root),
                    delete_db=True,
                    yes=True,
                )

        self.assertTrue(result["deleted"]["db_user"])
        self.assertEqual(sql[0], "DROP DATABASE IF EXISTS `baza_shop`")
        self.assertEqual(sql[1], "DROP USER IF EXISTS 'korisnik_shop'@'localhost'")

    def test_delete_removes_empty_parent_when_webroot_is_already_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            var_www = Path(tmp) / "var-www"
            parent = var_www / "shop"
            parent.mkdir(parents=True)
            webroot = parent / "public_html"
            plan = instance_delete.InstanceDeletePlan(
                root=webroot,
                local_php=None,
                domains=[],
                nginx_paths=[],
                db_name="",
                db_host="",
                db_port="",
                db_user="",
                db_password="",
                delete_files=True,
                delete_vhost=False,
                delete_db=False,
            )
            with (
                patch.object(instance_delete, "build_delete_plan", return_value=plan),
                patch.object(instance_delete, "_VAR_WWW", var_www),
                patch.object(instance_delete.os, "geteuid", return_value=0),
                patch("mcd_agent.inventory.InstanceInventory.rescan", return_value=0),
            ):
                result = instance_delete.delete_instance_artifacts(
                    SimpleNamespace(state_db_path=str(Path(tmp) / "state.db")),
                    root=str(webroot),
                    delete_files=True,
                    yes=True,
                )

        self.assertTrue(result["deleted"]["parent"])
        self.assertFalse(parent.exists())

    def test_delete_blocks_before_mail_cleanup_when_nginx_preflight_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "public_html"
            root.mkdir()
            nginx_path = Path(tmp) / "shop.example.com.conf"
            nginx_path.write_text("invalid", encoding="utf-8")
            plan = instance_delete.InstanceDeletePlan(
                root=root,
                local_php=None,
                domains=["shop.example.com"],
                nginx_paths=[nginx_path],
                db_name="",
                db_host="",
                db_port="",
                db_user="",
                db_password="",
                delete_files=True,
                delete_vhost=True,
                delete_db=False,
            )

            with (
                patch.object(instance_delete, "build_delete_plan", return_value=plan),
                patch.object(instance_delete.os, "geteuid", return_value=0),
                patch.object(instance_delete, "_run", return_value=(1, "broken config")),
                patch("mcd_agent.local_mail.disable_local_mail") as disable_mail,
            ):
                with self.assertRaisesRegex(RuntimeError, "nginx -t failed before"):
                    instance_delete.delete_instance_artifacts(
                        SimpleNamespace(state_db_path=str(Path(tmp) / "state.db")),
                        root=str(root),
                        domains=["shop.example.com"],
                        delete_files=True,
                        delete_vhost=True,
                        yes=True,
                    )

        disable_mail.assert_not_called()

    def test_delete_disables_local_mail_before_destructive_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "public_html"
            root.mkdir()
            plan = instance_delete.InstanceDeletePlan(
                root=root,
                local_php=None,
                domains=["shop.example.com"],
                nginx_paths=[],
                db_name="baza_shop",
                db_host="localhost",
                db_port="3306",
                db_user="shop",
                db_password="secret",
                delete_files=True,
                delete_vhost=True,
                delete_db=True,
            )
            order: list[str] = []

            def disable_mail(*_args: object, **_kwargs: object) -> dict[str, object]:
                order.append("mail")
                return {"status": "ok"}

            with (
                patch.object(instance_delete, "build_delete_plan", return_value=plan),
                patch.object(instance_delete.os, "geteuid", return_value=0),
                patch("mcd_agent.local_mail.disable_local_mail", side_effect=disable_mail),
                patch.object(instance_delete, "_mysql_exec", side_effect=lambda *_a, **_k: order.append("db")),
                patch.object(instance_delete, "_remove_instance_root", side_effect=lambda *_a, **_k: order.append("files")),
                patch("mcd_agent.inventory.InstanceInventory.rescan", return_value=0),
            ):
                result = instance_delete.delete_instance_artifacts(
                    SimpleNamespace(state_db_path=str(Path(tmp) / "state.db")),
                    root=str(root),
                    domains=["shop.example.com"],
                    delete_files=True,
                    delete_vhost=True,
                    delete_db=True,
                    yes=True,
                )

        self.assertEqual(order, ["mail", "db", "files"])
        self.assertEqual(result["deleted"]["local_mail"], ["shop.example.com"])

    def test_delete_can_use_absolute_root_after_inventory_row_is_gone(self) -> None:
        cfg = SimpleNamespace(state_db_path="/tmp/mcd-state.db")
        root = "/var/www/ss/public_html"
        with (
            patch.object(cli, "InstanceInventory", return_value=_Inventory([])),
            patch.object(cli, "ensure_seeded"),
        ):
            self.assertEqual(
                cli._select_root_for_ops(cfg, root, allow_missing_absolute=True),
                root,
            )

    def test_other_operations_still_require_inventory_match(self) -> None:
        cfg = SimpleNamespace(state_db_path="/tmp/mcd-state.db")
        with (
            patch.object(cli, "InstanceInventory", return_value=_Inventory([])),
            patch.object(cli, "ensure_seeded"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Mautic install not found"):
                cli._select_root_for_ops(cfg, "/var/www/ss/public_html")

    def test_instance_delete_main_allows_missing_absolute_root(self) -> None:
        calls: list[dict[str, object]] = []

        def select_root(_cfg: object, root: str | None, **kwargs: object) -> str:
            calls.append({"root": root, **kwargs})
            return str(root)

        with (
            patch.object(sys, "argv", [
                "mcd-cli",
                "instance-delete",
                "--root",
                "/var/www/ss/public_html",
                "--delete-files",
                "--yes",
                "--json",
            ]),
            patch.object(cli, "load_config", return_value=SimpleNamespace(state_db_path="/tmp/mcd-state.db")),
            patch.object(cli, "maybe_notify_update", return_value=None),
            patch.object(cli, "_select_root_for_ops", side_effect=select_root),
            patch.object(cli, "delete_instance_artifacts", return_value={"status": "ok"}),
            patch.object(cli, "_push_state_after_change"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.main(), 0)

        self.assertEqual(calls, [{"root": "/var/www/ss/public_html", "allow_missing_absolute": True}])

    def test_delete_plan_uses_inventory_db_when_local_php_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "public_html"
            root.mkdir()
            db_path = tmp_path / "mcd-state.db"
            inv = InstanceInventory(str(db_path))
            inv.add_or_update_manual(
                name="broken-site",
                root=str(root),
                console_path=str(root / "bin" / "console"),
                local_php_path=None,
                mautic_major=7,
                db_host="localhost",
                db_port=3306,
                db_name="baza_broken_site",
                db_user="korisnik_broken_site",
                db_password="secret",
                db_table_prefix="",
            )
            cfg = SimpleNamespace(state_db_path=str(db_path))

            with patch.object(instance_delete, "_safe_root", return_value=root.resolve(strict=False)):
                plan = instance_delete.build_delete_plan(cfg=cfg, root=str(root), delete_db=True)

        self.assertEqual(plan.db_name, "baza_broken_site")
        self.assertEqual(plan.db_host, "localhost")
        self.assertEqual(plan.db_user, "korisnik_broken_site")
        self.assertEqual(plan.db_password, "secret")

    def test_delete_plan_allows_explicit_db_name_without_local_php_db_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "public_html"
            root.mkdir()

            with patch.object(instance_delete, "_safe_root", return_value=root.resolve(strict=False)):
                plan = instance_delete.build_delete_plan(
                    root=str(root),
                    db_name="baza_partial_delete",
                    delete_db=True,
                )

        self.assertEqual(plan.db_name, "baza_partial_delete")
        self.assertEqual(plan.db_host, "")
        self.assertEqual(plan.db_user, "")

    def test_manual_command_main_still_requires_inventory_match(self) -> None:
        calls: list[dict[str, object]] = []

        def select_root(_cfg: object, root: str | None, **kwargs: object) -> str:
            calls.append({"root": root, **kwargs})
            return str(root)

        with (
            patch.object(sys, "argv", [
                "mcd-cli",
                "cache:clear",
                "--root",
                "/var/www/ss/public_html",
            ]),
            patch.object(cli, "load_config", return_value=SimpleNamespace(state_db_path="/tmp/mcd-state.db")),
            patch.object(cli, "maybe_notify_update", return_value=None),
            patch.object(cli, "_select_root_for_ops", side_effect=select_root),
            patch.object(cli, "_run_manual_command_with_scheduler", return_value=(0, "")),
            patch.object(cli, "_push_state_after_change"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.main(), 0)

        self.assertEqual(calls, [{"root": "/var/www/ss/public_html"}])


if __name__ == "__main__":
    unittest.main()
