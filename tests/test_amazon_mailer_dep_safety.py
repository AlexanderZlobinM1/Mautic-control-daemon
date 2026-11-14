from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcd_agent.amazon_mailer_dep import (
    _composer_project_is_mautic,
    _resolve_mailer_bridge_requirement,
    _composer_update_targeted_package,
    _ensure_composer_packages,
    ensure_composer_runtime_packages,
)


class MailerComposerSafetyTests(unittest.TestCase):
    def test_sendgrid_wildcard_tracks_installed_symfony_mailer_minor(self) -> None:
        proc = Mock(
            returncode=0,
            stdout=json.dumps({"versions": ["* v6.4.40"]}),
            stderr="",
        )
        with patch("mcd_agent.amazon_mailer_dep._run", return_value=proc) as run:
            requirement = _resolve_mailer_bridge_requirement(
                project_root="/var/www/mautic",
                composer_bin="composer",
                package_name="symfony/sendgrid-mailer:*",
            )

        self.assertEqual(requirement, "symfony/sendgrid-mailer:^6.4")
        self.assertIn("symfony/mailer", run.call_args.args[0])

    def test_amazon_bridge_fails_closed_without_installed_mailer_version(self) -> None:
        proc = Mock(returncode=0, stdout=json.dumps({"versions": []}), stderr="")
        with patch("mcd_agent.amazon_mailer_dep._run", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "Cannot parse"):
                _resolve_mailer_bridge_requirement(
                    project_root="/var/www/mautic",
                    composer_bin="composer",
                    package_name="symfony/amazon-mailer",
                )

    def test_rejects_unrelated_composer_project_created_by_mailer_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "composer.json").write_text(
                json.dumps({"require": {"symfony/sendgrid-mailer": "*"}}),
                encoding="utf-8",
            )
            self.assertFalse(_composer_project_is_mautic(str(root)))

    def test_accepts_mautic_composer_project(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "composer.json").write_text(
                json.dumps({"name": "mautic/recommended-project", "require": {"mautic/core-lib": "^4"}}),
                encoding="utf-8",
            )
            self.assertTrue(_composer_project_is_mautic(str(root)))

    def test_runtime_package_wrapper_disables_composer_scripts(self) -> None:
        cfg = SimpleNamespace()
        with patch("mcd_agent.amazon_mailer_dep._ensure_composer_packages", return_value=True) as ensure:
            changed = ensure_composer_runtime_packages(
                config=cfg,
                root="/var/www/mautic",
                console_path="/var/www/mautic/bin/console",
                packages={"nikic/php-parser:^5.0"},
                reason="plugin-registration",
            )

        self.assertTrue(changed)
        ensure.assert_called_once_with(
            config=cfg,
            root="/var/www/mautic",
            console_path="/var/www/mautic/bin/console",
            required={"nikic/php-parser:^5.0"},
            reason="plugin-registration",
            ensure_node=False,
            no_scripts=True,
        )

    def test_runtime_composer_require_uses_no_scripts(self) -> None:
        cfg = SimpleNamespace(php_bin="php", command_timeout_sec=900)
        with (
            patch("mcd_agent.amazon_mailer_dep._resolve_project_root", return_value="/var/www/mautic"),
            patch("mcd_agent.amazon_mailer_dep.detect_install_type", return_value="composer"),
            patch("mcd_agent.amazon_mailer_dep._composer_project_is_mautic", return_value=True),
            patch("mcd_agent.amazon_mailer_dep._mautic_console_healthy", side_effect=[True, True]),
            patch(
                "mcd_agent.amazon_mailer_dep.run_upgrade_os_operation",
                return_value={
                    "status": "ok",
                    "composer_binary": "/usr/local/bin/composer",
                    "composer_dependency_commands_owned_by_mcd": True,
                },
            ) as host_prepare,
            patch("mcd_agent.amazon_mailer_dep._verify_composer_as_www_data"),
            patch("mcd_agent.amazon_mailer_dep._composer_has_package", return_value=False),
            patch("mcd_agent.amazon_mailer_dep._composer_update_targeted_package") as targeted,
            patch("mcd_agent.amazon_mailer_dep._run", return_value=Mock(returncode=0)),
        ):
            changed = _ensure_composer_packages(
                config=cfg,
                root="/var/www/mautic",
                console_path="/var/www/mautic/bin/console",
                required={"nikic/php-parser:^5.0"},
                reason="plugin-registration",
                ensure_node=False,
                no_scripts=True,
            )

        self.assertTrue(changed)
        self.assertEqual(host_prepare.call_args.kwargs["component"], "composer")
        self.assertEqual(host_prepare.call_args.kwargs["operation"], "composer_host_prepare")
        self.assertEqual(host_prepare.call_args.kwargs["operation_input"]["working_root"], "/var/www/mautic")
        targeted.assert_called_once_with(
            project_root="/var/www/mautic",
            composer_bin="/usr/local/bin/composer",
            package_name="nikic/php-parser:^5.0",
            timeout_sec=900,
        )

    def test_targeted_update_restores_private_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original = {
                "repositories": [{"type": "vcs", "url": "https://git.sales-snap.com/private.git"}],
                "require": {"mautic/core-lib": "^7.0"},
            }
            (root / "composer.json").write_text(json.dumps(original), encoding="utf-8")
            (root / "composer.lock").write_text("original", encoding="utf-8")

            def fake_run(cmd, **kwargs):
                if cmd[1] == "require":
                    data = json.loads((root / "composer.json").read_text(encoding="utf-8"))
                    data.setdefault("require", {})["nikic/php-parser"] = "^5.0"
                    (root / "composer.json").write_text(json.dumps(data), encoding="utf-8")
                if cmd[1] == "update":
                    (root / "composer.lock").write_text("updated", encoding="utf-8")
                return Mock(returncode=0)

            with patch("mcd_agent.amazon_mailer_dep._run", side_effect=fake_run) as run:
                _composer_update_targeted_package(
                    project_root=str(root), composer_bin="composer",
                    package_name="nikic/php-parser:^5.0", timeout_sec=30,
                )

            updated = json.loads((root / "composer.json").read_text())
            self.assertEqual(updated["repositories"], original["repositories"])
            self.assertEqual(updated["require"]["mautic/core-lib"], "^7.0")
            self.assertEqual(updated["require"]["nikic/php-parser"], "^5.0")
            self.assertEqual((root / "composer.lock").read_text(), "updated")
            self.assertEqual(run.call_count, 2)

    def test_targeted_update_restores_json_and_lock_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original_json = '{"repositories":[{"type":"vcs","url":"https://git.sales-snap.com/private.git"}]}\n'
            (root / "composer.json").write_text(original_json, encoding="utf-8")
            (root / "composer.lock").write_text("original", encoding="utf-8")

            def fail_run(cmd, **kwargs):
                if cmd[1] == "require":
                    (root / "composer.json").write_text('{"require":{"nikic/php-parser":"^5.0"}}', encoding="utf-8")
                if cmd[1] == "update":
                    (root / "composer.lock").write_text("partial", encoding="utf-8")
                    raise RuntimeError("update failed")
                return Mock(returncode=0)

            with patch("mcd_agent.amazon_mailer_dep._run", side_effect=fail_run):
                with self.assertRaises(RuntimeError):
                    _composer_update_targeted_package(
                        project_root=str(root), composer_bin="composer",
                        package_name="nikic/php-parser:^5.0", timeout_sec=30,
                    )

            self.assertEqual((root / "composer.json").read_text(encoding="utf-8"), original_json)
            self.assertEqual((root / "composer.lock").read_text(encoding="utf-8"), "original")


if __name__ == "__main__":
    unittest.main()
