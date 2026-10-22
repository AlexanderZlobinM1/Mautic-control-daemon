import tempfile
import unittest
from pathlib import Path

from mcd_agent.backup import _sync_profile_payload_to_config
from mcd_agent.config import _preserve_existing_backup_schedule


class BackupProfileScheduleSyncTests(unittest.TestCase):
    def test_nested_schedule_is_written_to_effective_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcd.local.toml"
            path.write_text("[backup]\nenabled = true\n", encoding="utf-8")
            changed = _sync_profile_payload_to_config(
                type("Cfg", (), {"config_file_path": str(path)})(),
                {"schedule": {"enabled": True, "interval_sec": 86400, "quiet_hour": 2, "quiet_minute": 7, "quiet_window_min": 60, "pre_pause_sec": 3600}},
            )
            text = path.read_text(encoding="utf-8")
            self.assertTrue(changed)
            self.assertIn("[backup.schedule]", text)
            self.assertIn("quiet_minute = 7", text)

    def test_bootstrap_preserves_existing_schedule_when_desired_omits_it(self):
        existing = "[backup.schedule]\nenabled = true\ninterval_sec = 86400\nquiet_hour = 2\nquiet_minute = 7\nquiet_window_min = 60\npre_pause_sec = 3600\n"
        desired = "[mcc]\nurl = \"https://mcc.example\"\ntoken = \"x\"\n"
        merged = _preserve_existing_backup_schedule(existing, desired)
        self.assertIn("[backup.schedule]", merged)
        self.assertIn("quiet_minute = 7", merged)

    def test_iso_timestamp_is_rejected(self):
        from mcd_agent.backup import backup_profile_apply_sync, backup_profile_sync_hash
        result = backup_profile_apply_sync(object(), {"schema": "mcd-backup-profile-v1", "version": 1, "profile_updated_at": "2026-10-22T20:00:00Z", "profile": {}, "profile_sha256": backup_profile_sync_hash({})})[1]
        self.assertEqual(result["reason"], "invalid_updated_at")


if __name__ == "__main__":
    unittest.main()
