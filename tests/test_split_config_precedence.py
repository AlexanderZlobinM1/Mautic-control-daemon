import tempfile
import unittest
from pathlib import Path

from mcd_agent.config import _load_toml_with_includes


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


if __name__ == "__main__":
    unittest.main()
