from __future__ import annotations

import tempfile
import json
import unittest
from pathlib import Path

from mcd_agent.install_type import (
    app_bundle_dir_candidates,
    detect_install_type,
    is_complete_plugin_bundle,
    plugin_dir_candidates,
)


class InstallTypePathTests(unittest.TestCase):
    def test_zip_with_composer_files_and_split_layout_stays_zip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for directory in ("config", "bin", "app", "plugins", "docroot", "public"):
                (root / directory).mkdir()
            (root / "config/local.php").write_text("<?php")
            (root / "bin/console").write_text("<?php")
            (root / "composer.json").write_text(json.dumps({
                "name": "mautic/core", "repositories": [{"type": "path", "url": "app"}]}))
            (root / "composer.lock").write_text(json.dumps({
                "packages": [{"name": "mautic/core-lib", "version": "6.0.0-dev"}]}))
            self.assertEqual(detect_install_type(str(root)), "zip")
            for webroot in ("docroot", "public"):
                self.assertEqual(detect_install_type(str(root / webroot)), "zip")
            from unittest.mock import patch
            from mcd_agent.mautic_upgrade import _apply_zip
            package = root.parent / (root.name + "-6.0.10-update.zip")
            package.write_bytes(b"verified package fixture")
            try:
                with patch("mcd_agent.mautic_upgrade._resolve_update_package", return_value=package), patch(
                    "mcd_agent.mautic_upgrade._run"
                ) as run:
                    _apply_zip(None, str(root), "bin/console", "php", "6.0.10")
                self.assertEqual(run.call_args_list[0].args[0], [
                    "php", "bin/console", "mautic:update:apply", "--force",
                    "--update-package=" + package.name])
                self.assertEqual(run.call_args_list[-1].args[0], [
                    "php", "bin/console", "mautic:update:apply", "--finish"])
            finally:
                package.unlink()

    def test_recommended_project_is_explicit_and_ambiguous_ownership_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            webroot = root / "docroot"
            webroot.mkdir()
            manifest = json.dumps({"name": "mautic/recommended-project"})
            (root / "composer.json").write_text(manifest)
            self.assertEqual(detect_install_type(str(root)), "composer")
            self.assertEqual(detect_install_type(str(webroot)), "composer")
            (webroot / "composer.json").write_text(manifest)
            self.assertEqual(detect_install_type(str(webroot)), "unknown")

    def test_composer_layout_prefers_docroot_mutable_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "config" / "local.php").write_text("<?php $parameters = [];\n", encoding="utf-8")
            (root / "bin").mkdir()
            (root / "bin" / "console").write_text("", encoding="utf-8")
            (root / "docroot").mkdir()

            self.assertEqual(plugin_dir_candidates(root)[0], root / "docroot" / "plugins")
            self.assertEqual(app_bundle_dir_candidates(root)[0], root / "docroot" / "app" / "bundles")

    def test_zip_layout_prefers_root_mutable_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            self.assertEqual(plugin_dir_candidates(root)[0], root / "plugins")
            self.assertEqual(app_bundle_dir_candidates(root)[0], root / "app" / "bundles")

    def test_complete_plugin_bundle_requires_metadata_and_matching_entry_class(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bundle = Path(td) / "MultiCaptchaBundle"
            (bundle / "Config").mkdir(parents=True)
            (bundle / "Config" / "config.php").write_text("<?php return [];\n", encoding="utf-8")
            (bundle / "MultiCaptchaBundle.php").write_text(
                "<?php class MultiCaptchaBundle extends PluginBundleBase {}\n", encoding="utf-8"
            )
            self.assertTrue(is_complete_plugin_bundle(bundle, "MultiCaptchaBundle"))

            (bundle / "MultiCaptchaBundle.php").unlink()
            self.assertFalse(is_complete_plugin_bundle(bundle, "MultiCaptchaBundle"))

            (bundle / "MultiCaptchaBundle.php").write_text(
                "<?php class DifferentBundle extends PluginBundleBase {}\n", encoding="utf-8"
            )
            self.assertFalse(is_complete_plugin_bundle(bundle, "MultiCaptchaBundle"))

    def test_docker_descriptor_layout_never_falls_back_to_zip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plugins = root / "plugins"
            plugins.mkdir()
            descriptor = type(
                "Descriptor",
                (),
                {
                    "install_type": "composer",
                    "host_plugins_path": plugins,
                    "has_capability": lambda _self, name: name in {"plugin-read", "plugin-write"},
                },
            )()
            from unittest.mock import patch

            with patch("mcd_agent.install_type.descriptor_for_root", return_value=descriptor):
                self.assertEqual(detect_install_type(str(root)), "composer")
                self.assertEqual(plugin_dir_candidates(root), [plugins])
                self.assertEqual(app_bundle_dir_candidates(root), [])

    def test_docker_descriptor_without_layout_is_unknown_not_zip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            descriptor = type(
                "Descriptor",
                (),
                {"install_type": None, "host_plugins_path": None},
            )()
            from unittest.mock import patch

            with patch("mcd_agent.install_type.descriptor_for_root", return_value=descriptor):
                self.assertEqual(detect_install_type(str(root)), "unknown")


if __name__ == "__main__":
    unittest.main()
