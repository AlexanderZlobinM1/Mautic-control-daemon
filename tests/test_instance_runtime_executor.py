from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent.cli import _run_manual_command_with_scheduler
from mcd_agent.executor import build_mautic_exec_args


class InstanceRuntimeExecutorTest(unittest.TestCase):
    def test_uses_configured_php_even_when_legacy_wrapper_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "bin").mkdir()
            (root / "bin" / "console").write_text("#!/usr/bin/env php\n", encoding="utf-8")
            wrapper = root / ".mcd" / "php"
            wrapper.parent.mkdir()
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)

            cmd = build_mautic_exec_args(
                php_bin="/usr/bin/php",
                root=str(root),
                command="cache:clear",
                instance_id=None,
                run_as_user=None,
            )

        self.assertEqual(cmd[0], "/usr/bin/php")

    def test_falls_back_when_instance_php_wrapper_is_not_executable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "bin").mkdir()
            (root / "bin" / "console").write_text("#!/usr/bin/env php\n", encoding="utf-8")
            wrapper = root / ".mcd" / "php"
            wrapper.parent.mkdir()
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o700)

            cmd = build_mautic_exec_args(
                php_bin="/usr/bin/php",
                root=str(root),
                command="cache:clear",
                instance_id=None,
                run_as_user=None,
            )

        self.assertEqual(cmd[0], "/usr/bin/php")

    def test_campaign_shorthand_uses_native_plural_mautic_commands(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "bin").mkdir()
            (root / "bin" / "console").write_text("#!/usr/bin/env php\n", encoding="utf-8")

            rebuild = build_mautic_exec_args(
                php_bin="/usr/bin/php",
                root=str(root),
                command="campaign:rebuild",
                instance_id=8,
                run_as_user="www-data",
            )
            trigger = build_mautic_exec_args(
                php_bin="/usr/bin/php",
                root=str(root),
                command="campaign:trigger",
                instance_id=8,
                run_as_user="www-data",
            )
            explicit_rebuild = build_mautic_exec_args(
                php_bin="/usr/bin/php",
                root=str(root),
                command="campaigns:rebuild",
                instance_id=None,
                run_as_user=None,
            )

        self.assertIn("mautic:campaigns:rebuild", rebuild)
        self.assertIn("mautic:campaigns:trigger", trigger)
        self.assertIn("mautic:campaigns:rebuild", explicit_rebuild)
        self.assertIn("-i", rebuild)
        self.assertIn("8", rebuild)
        self.assertNotIn("-i", explicit_rebuild)

    def test_manual_local_exec_runs_synchronously_instead_of_queueing(self) -> None:
        cfg = SimpleNamespace(
            cluster_id="",
            cluster_routing_enabled=False,
            command_timeout_sec=1800,
            dispatch_interval_sec=1,
            state_db_path="/tmp/unused.db",
            profile_name="midi",
        )
        with patch("mcd_agent.cli.execute_mautic_command", return_value=(0, "native output")) as run, patch(
            "mcd_agent.cli.TaskStore"
        ) as store:
            rc, output = _run_manual_command_with_scheduler(
                cfg=cfg,
                php_bin="/usr/bin/php",
                root="/var/www/example/public_html",
                command="campaign:rebuild",
                instance_id=8,
                timeout_sec=1800,
                run_as_user="www-data",
            )

        self.assertEqual(rc, 0)
        self.assertEqual(output, "native output")
        run.assert_called_once_with(
            php_bin="/usr/bin/php",
            root="/var/www/example/public_html",
            command="campaign:rebuild",
            instance_id=8,
            timeout_sec=1800,
            run_as_user="www-data",
        )
        store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
