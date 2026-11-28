from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent.assetmapper_verification import discover_asset_webroot, verify_assetmapper_upgrade


class AssetMapperVerificationTests(unittest.TestCase):
    def test_backup_off_runs_assets_and_preserves_unavailable_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.php").write_text("<?php")
            build = root / "assets/build"
            build.mkdir(parents=True)
            (build / "app.css").write_text("body{}")
            (build / "manifest.json").write_text(json.dumps({"css": "/assets/build/app.css"}))
            calls = []
            def runner(command, **kwargs):
                calls.append(command)
                content_type = "application/json" if command[-1].endswith("manifest.json") else "text/css"
                return subprocess.CompletedProcess(command, 0, f"HTTP/2 200\r\ncontent-type: {content_type}\r\n", "")
            result = verify_assetmapper_upgrade(
                project_root=root, install_root=root, console_path=root / "bin/console",
                php_bin="php", runtime_user="www-data", domain="example.test",
                target_version="7.2.1", rollback_available=False, runner=runner)
            self.assertEqual(result["status"], "success")
            self.assertFalse(result["rollback"]["available"])
            self.assertFalse(result["rollback"]["attempted"])
            self.assertEqual([item["name"] for item in result["commands"]],
                             ["mautic:assets:generate", "cache:clear"])
            self.assertEqual(len(calls), 4)

    def test_backup_off_real_command_failure_is_not_success_or_fake_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.php").write_text("<?php")
            calls = []
            def runner(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 1, "", "private diagnostic")
            result = verify_assetmapper_upgrade(
                project_root=root, install_root=root, console_path=root / "bin/console",
                php_bin="php", runtime_user="www-data", domain="example.test",
                target_version="7.2.1", rollback_available=False, runner=runner)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason"], "mautic:assets:generate failed")
            self.assertEqual(result["rollback"], {"available": False, "required": True, "attempted": False})
            self.assertEqual(len(calls), 1)
            self.assertNotIn("private diagnostic", json.dumps(result))

    def test_composer_layout_uses_declared_nonstandard_webroot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            webroot = project / "docroot"
            webroot.mkdir(parents=True)
            (project / "composer.json").write_text(
                json.dumps({"extra": {"mautic-scaffold": {"locations": {"web-root": "docroot/"}}}}),
                encoding="utf-8",
            )
            (webroot / "index.php").write_text("<?php", encoding="utf-8")
            self.assertEqual(discover_asset_webroot(project, webroot), webroot.resolve())

    def test_zip_layout_uses_project_root_public_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "zip"
            project.mkdir()
            (project / "composer.json").write_text(json.dumps({"extra": {"public-dir": "."}}), encoding="utf-8")
            (project / "index.php").write_text("<?php", encoding="utf-8")
            self.assertEqual(discover_asset_webroot(project, project), project.resolve())

    def test_verifies_manifest_files_and_http_content_types(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            webroot = project / "docroot"
            build = webroot / "assets" / "build"
            build.mkdir(parents=True)
            (project / "composer.json").write_text(
                json.dumps({"extra": {"mautic-scaffold": {"locations": {"web-root": "docroot/"}}}}),
                encoding="utf-8",
            )
            (webroot / "index.php").write_text("<?php", encoding="utf-8")
            (build / "manifest.json").write_text(
                json.dumps({"app": {"css": "/assets/build/css/app.css", "js": "assets/build/app.js"}}),
                encoding="utf-8",
            )
            (build / "css").mkdir()
            (build / "css" / "app.css").write_text("body{}", encoding="utf-8")
            (build / "app.js").write_text("console.log(1);", encoding="utf-8")

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "curl":
                    if command[-1].endswith("manifest.json"):
                        content_type = "application/json"
                    elif command[-1].endswith(".css"):
                        content_type = "text/css"
                    else:
                        content_type = "application/javascript"
                    return subprocess.CompletedProcess(command, 0, f"HTTP/2 200\r\ncontent-type: {content_type}\r\n\r\n", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            result = verify_assetmapper_upgrade(
                project_root=project,
                install_root=webroot,
                console_path=project / "bin" / "console",
                php_bin="php",
                runtime_user="www-data",
                domain="example.test",
                target_version="7.2.0",
                runner=runner,
            )
            self.assertEqual(result["status"], "success")
            self.assertFalse(result["rollback_required"])
            self.assertEqual(result["asset_count"], 2)
            self.assertEqual(result["schema"], "mcd-mautic-assetmapper-verification-v2")
            self.assertEqual(result["webroot_source"], "composer")
            self.assertEqual(result["manifest"]["http_status"], 200)
            self.assertTrue(result["rollback"]["available"])
            self.assertTrue(all(asset["status"] == "ok" for asset in result["assets"]))

    def test_uses_detected_local_http_origin_with_actual_port(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.php").write_text("<?php", encoding="utf-8")
            build = root / "assets" / "build"
            build.mkdir(parents=True)
            (build / "manifest.json").write_text(
                json.dumps({"css": "/assets/build/app.css"}),
                encoding="utf-8",
            )
            (build / "app.css").write_text("body{}", encoding="utf-8")
            curl_commands: list[list[str]] = []

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if command[0] != "curl":
                    return subprocess.CompletedProcess(command, 0, "", "")
                curl_commands.append(command)
                if command[-1].startswith("http://127.0.0.1:61340"):
                    return subprocess.CompletedProcess(command, 7, "", "connection refused")
                content_type = "application/json" if command[-1].endswith("manifest.json") else "text/css"
                return subprocess.CompletedProcess(
                    command,
                    0,
                    f"HTTP/1.1 200 OK\r\ncontent-type: {content_type}\r\n\r\n",
                    "",
                )

            result = verify_assetmapper_upgrade(
                project_root=root,
                install_root=root,
                console_path=root / "bin" / "console",
                php_bin="php",
                runtime_user="www-data",
                target_version="7.2.3",
                probe_origins=["http://127.0.0.1:61340", "http://127.0.0.1:8080"],
                runner=runner,
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["selected_origin"], "http://127.0.0.1:8080")
            self.assertEqual(len(result["manifest"]["attempts"]), 2)
            self.assertTrue(curl_commands[-1][-1].startswith("http://127.0.0.1:8080/"))
            self.assertTrue(all("--resolve" not in command for command in curl_commands))

    def test_production_origin_keeps_local_sni_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.php").write_text("<?php", encoding="utf-8")
            build = root / "assets" / "build"
            build.mkdir(parents=True)
            (build / "manifest.json").write_text(json.dumps({"css": "/assets/build/app.css"}), encoding="utf-8")
            (build / "app.css").write_text("body{}", encoding="utf-8")
            curl_commands: list[list[str]] = []

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "curl":
                    curl_commands.append(command)
                    content_type = "application/json" if command[-1].endswith("manifest.json") else "text/css"
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        f"HTTP/2 200\r\ncontent-type: {content_type}\r\n\r\n",
                        "",
                    )
                return subprocess.CompletedProcess(command, 0, "", "")

            result = verify_assetmapper_upgrade(
                project_root=root,
                install_root=root,
                console_path=root / "bin" / "console",
                php_bin="php",
                runtime_user="www-data",
                target_version="7.2.3",
                domain="mautic.example.test",
                runner=runner,
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["selected_origin"], "https://mautic.example.test")
            self.assertTrue(curl_commands)
            self.assertTrue(
                all(
                    command[command.index("--resolve") + 1] == "mautic.example.test:443:127.0.0.1"
                    for command in curl_commands
                )
            )

    def test_rejects_html_response_for_css(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            webroot = project / "docroot"
            build = webroot / "assets" / "build"
            build.mkdir(parents=True)
            (project / "composer.json").write_text(json.dumps({"extra": {"public-dir": "docroot"}}), encoding="utf-8")
            (webroot / "index.php").write_text("<?php", encoding="utf-8")
            (build / "manifest.json").write_text(json.dumps({"css": "/assets/build/app.css"}), encoding="utf-8")
            (build / "app.css").write_text("body{}", encoding="utf-8")

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "curl":
                    content_type = "application/json" if command[-1].endswith("manifest.json") else "text/html"
                    return subprocess.CompletedProcess(command, 0, f"HTTP/2 200\r\ncontent-type: {content_type}\r\n\r\n", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            result = verify_assetmapper_upgrade(
                project_root=project,
                install_root=webroot,
                console_path=project / "bin" / "console",
                php_bin="php",
                runtime_user="www-data",
                domain="example.test",
                target_version="7.2.0",
                runner=runner,
            )
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["rollback_required"])
            self.assertIn("content type", result["reason"])
