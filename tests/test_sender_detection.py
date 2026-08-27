from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from mcd_agent.state_push import _collect_installed_plugins, _detect_sender_profile


class SenderDetectionTests(unittest.TestCase):
    def _write_local_php(self, root: Path, body: str) -> None:
        config = root / "config"
        config.mkdir(parents=True)
        (config / "local.php").write_text("<?php $parameters = [" + body + "];\n", encoding="utf-8")

    def test_own_host_sendmail_is_detected_from_local_php(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_local_php(
                root,
                "'mailer_dsn'=>'sendmail://default?command=%2Fusr%2Flocal%2Fbin%2Fmcd-mail-submit',"
                "'mailer_from_email'=>'mailer@app.example.com','mailer_return_path'=>'bounce@app.example.com'",
            )

            result = _detect_sender_profile(str(root), [])

        self.assertEqual(result["sender_key"], "own_host")
        self.assertEqual(result["sender_type"], "own host")
        self.assertTrue(result["sender_config"]["configured"])
        self.assertEqual(result["sender_config"]["from_email"], "mailer@app.example.com")

    def test_zender_plugin_is_not_classified_as_email_sender(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_local_php(root, "'mailer_dsn'=>'','mailer_from_email'=>''")

            result = _detect_sender_profile(str(root), [{"bundle": "MauticZenderBundle", "version": "1.2.10"}])

        self.assertEqual(result["sender_key"], "unknown")
        self.assertEqual(result["sender_type"], "unknown")
        self.assertFalse(result["sender_config"]["configured"])
        self.assertNotIn("Zender", result["sender_title"])

    def test_ses_snapshot_contains_only_safe_configuration_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_local_php(
                root,
                "'mailer_dsn'=>'ses+api://ACCESS:SECRET@default?region=eu-central-1',"
                "'mailer_from_email'=>'mailer@app.example.com','mailer_from_name'=>'Sales Snap',"
                "'mailer_return_path'=>'bounce@app.example.com'",
            )

            result = _detect_sender_profile(str(root), [])

        observed = result["sender_config"]
        self.assertEqual(result["sender_type"], "ses+api")
        self.assertEqual(observed["region"], "eu-central-1")
        self.assertEqual(observed["credentials_present"], {"username": True, "password": True})
        self.assertNotIn("ACCESS", str(observed))
        self.assertNotIn("SECRET", str(observed))

    def test_plugin_inventory_ignores_incomplete_removed_bundle_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugins = root / "plugins"
            complete = plugins / "MultiCaptchaBundle"
            (complete / "Config").mkdir(parents=True)
            (complete / "Config" / "config.php").write_text("<?php return ['version' => '2.1.0'];\n", encoding="utf-8")
            (complete / "MultiCaptchaBundle.php").write_text(
                "<?php class MultiCaptchaBundle extends PluginBundleBase {}\n", encoding="utf-8"
            )

            config_only = plugins / "RemovedConfigOnlyBundle" / "Config"
            config_only.mkdir(parents=True)
            (config_only / "config.php").write_text("<?php return [];\n", encoding="utf-8")

            class_only = plugins / "RemovedClassOnlyBundle"
            class_only.mkdir()
            (class_only / "RemovedClassOnlyBundle.php").write_text(
                "<?php class RemovedClassOnlyBundle extends PluginBundleBase {}\n", encoding="utf-8"
            )

            self.assertEqual(
                _collect_installed_plugins(str(root)),
                [{"bundle": "MultiCaptchaBundle", "version": "2.1.0"}],
            )

    def test_plugin_inventory_ignores_entry_file_without_matching_bundle_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "plugins" / "BrokenBundle"
            (bundle / "Config").mkdir(parents=True)
            (bundle / "Config" / "config.php").write_text("<?php return ['version' => '1.0.0'];\n", encoding="utf-8")
            (bundle / "BrokenBundle.php").write_text("<?php class OtherBundle {}\n", encoding="utf-8")

            self.assertEqual(_collect_installed_plugins(str(root)), [])


if __name__ == "__main__":
    unittest.main()
