import tempfile
import unittest
from pathlib import Path

from mcd_agent.config import (
    _load_toml_with_includes,
    retire_split_entrypoint_runtime_values,
    upsert_runtime_values,
    upsert_section_values,
)
from mcd_agent.mode import _resolve_mutable_config_path as resolve_mode_mutable_config_path


class SplitConfigPrecedenceTests(unittest.TestCase):
    def test_local_backup_settings_override_entrypoint_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mcd.toml"
            local = Path(tmp) / "mcd.local.toml"
            root.write_text(
                '[include]\nfiles = ["mcd.local.toml"]\n\n[backup]\nenabled = false\n[backup.schedule]\nenabled = false\ninterval_sec = 86400\n',
                encoding="utf-8",
            )
            local.write_text(
                '[backup]\nenabled = true\n[backup.schedule]\nenabled = true\nquiet_hour = 2\nquiet_minute = 0\n',
                encoding="utf-8",
            )
            effective = _load_toml_with_includes(str(root))
            self.assertTrue(effective["backup"]["enabled"])
            self.assertTrue(effective["backup"]["schedule"]["enabled"])
            self.assertEqual(effective["backup"]["schedule"]["interval_sec"], 86400)

    def test_repeated_runtime_upsert_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "mcd.local.toml"
            original = '[runtime]\nsegment_limit = 7 # operator formatting\nother = "keep"\n'
            local.write_text(original, encoding="utf-8")

            path, changed = upsert_section_values(
                str(local), "runtime", {"segment_limit": 7}
            )

            self.assertEqual(Path(path).resolve(), local.resolve())
            self.assertFalse(changed)
            self.assertEqual(local.read_text(encoding="utf-8"), original)

    def test_unmarked_split_entrypoint_writes_local_and_retires_misplaced_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mcd.toml"
            local = Path(tmp) / "mcd.local.toml"
            root.write_text(
                '[runtime]\n'
                'mcd_update_policy = "approved"\n\n'
                '# MCC-managed public MCD root entrypoint.\n'
                '[include]\nfiles = ["system.toml", "mcd.local.toml"]\n',
                encoding="utf-8",
            )
            (Path(tmp) / "system.toml").write_text(
                '[runtime]\nmcd_update_policy = "approved"\n',
                encoding="utf-8",
            )
            local.write_text('[profile]\nname = "passive"\n', encoding="utf-8")

            path, changed = upsert_runtime_values(
                str(root),
                {"mcd_update_policy": "off"},
            )

            self.assertEqual(Path(path).resolve(), local.resolve())
            self.assertTrue(changed)
            self.assertNotIn("mcd_update_policy", root.read_text(encoding="utf-8"))
            self.assertEqual(
                _load_toml_with_includes(str(root))["runtime"]["mcd_update_policy"],
                "off",
            )
            self.assertEqual(resolve_mode_mutable_config_path(str(root)).resolve(), local.resolve())

            root_text = root.read_text(encoding="utf-8").replace(
                '[runtime]\n',
                '[runtime]\nmessage_queue_instance_settings = { "one.example" = { enabled = false } }\n',
                1,
            )
            root.write_text(root_text, encoding="utf-8")
            retired = retire_split_entrypoint_runtime_values(
                str(root),
                {"mcd_update_policy", "message_queue_instance_settings"},
            )
            self.assertEqual(retired, 1)
            self.assertNotIn("message_queue_instance_settings", root.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
