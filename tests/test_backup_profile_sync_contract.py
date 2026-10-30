import unittest
from unittest.mock import patch

from mcd_agent.backup import (
    BACKUP_PROFILE_SYNC_SCHEMA,
    BACKUP_PROFILE_SYNC_VERSION,
    backup_profile_apply_sync,
    backup_profile_sync_hash,
)


class BackupProfileSyncContractTests(unittest.TestCase):
    def test_hash_mismatch_is_unsynced_without_apply(self):
        cfg = object()
        envelope = {
            "schema": BACKUP_PROFILE_SYNC_SCHEMA,
            "version": BACKUP_PROFILE_SYNC_VERSION,
            "profile_updated_at": 4,
            "profile": {"enabled": True},
            "profile_sha256": "bad",
        }
        with patch("mcd_agent.backup.backup_profile_set") as apply:
            _, result = backup_profile_apply_sync(cfg, envelope)
        self.assertEqual(result["status"], "unsynced")
        self.assertEqual(result["reason"], "desired_hash_mismatch")
        apply.assert_not_called()

    def test_valid_envelope_returns_masked_effective_readback(self):
        cfg = object()
        profile = {"enabled": True, "storage": {"password": "secret"}}
        envelope = {
            "schema": BACKUP_PROFILE_SYNC_SCHEMA,
            "version": BACKUP_PROFILE_SYNC_VERSION,
            "profile_updated_at": 7,
            "profile": profile,
            "profile_sha256": backup_profile_sync_hash(profile),
        }
        with patch("mcd_agent.backup._backup_profile_effective_values", return_value=profile), patch("mcd_agent.backup._cfg_with_profile_payload", return_value=cfg), patch(
            "mcd_agent.backup._validate_cfg"
        ), patch("mcd_agent.backup.backup_profile_set"), patch(
            "mcd_agent.backup.backup_profile_for_push", return_value=profile
        ), patch(
            "mcd_agent.backup.backup_profile_get", return_value={}
        ):
            _, result = backup_profile_apply_sync(cfg, envelope)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["profile_updated_at"], 7)
        self.assertEqual(result["effective_sha256"], envelope["profile_sha256"])
        self.assertNotEqual(result["effective_profile"]["storage"]["password"], "secret")

    def test_unsupported_schema_never_claims_ok(self):
        _, result = backup_profile_apply_sync(object(), {"schema": "other", "version": 1})
        self.assertEqual(result["status"], "unsynced")
        self.assertEqual(result["reason"], "unsupported_schema")

    def test_host_newer_wins(self):
        cfg = object()
        profile = {"enabled": True}
        envelope = {"schema": BACKUP_PROFILE_SYNC_SCHEMA, "version": 1, "profile_updated_at": 20,
                    "profile": profile, "profile_sha256": backup_profile_sync_hash(profile)}
        with patch("mcd_agent.backup._backup_profile_effective_values", return_value=profile), patch("mcd_agent.backup.backup_profile_get", return_value={"_profile_updated_at": 10}), patch(
            "mcd_agent.backup.backup_profile_for_push", return_value={"enabled": False}
        ), patch("mcd_agent.backup._cfg_with_profile_payload", return_value=cfg), patch(
            "mcd_agent.backup._validate_cfg"
        ), patch("mcd_agent.backup.backup_profile_set"), patch("mcd_agent.backup.backup_profile_for_push", return_value=profile):
            _, result = backup_profile_apply_sync(cfg, envelope)
        self.assertEqual(result["status"], "ok")

    def test_mcc_newer_wins(self):
        cfg = object()
        profile = {"enabled": True}
        envelope = {"schema": BACKUP_PROFILE_SYNC_SCHEMA, "version": 1, "profile_updated_at": 20,
                    "profile": profile, "profile_sha256": backup_profile_sync_hash(profile)}
        with patch("mcd_agent.backup._backup_profile_effective_values", return_value=profile), patch("mcd_agent.backup.backup_profile_get", return_value={"_profile_updated_at": 10}), patch("mcd_agent.backup.backup_profile_for_push", return_value={"enabled": False}), patch("mcd_agent.backup._cfg_with_profile_payload", return_value=cfg), patch("mcd_agent.backup._validate_cfg"), patch("mcd_agent.backup.backup_profile_set"), patch("mcd_agent.backup.backup_profile_for_push", return_value=profile):
            _, result = backup_profile_apply_sync(cfg, envelope)
        self.assertEqual(result["status"], "ok")

    def test_equal_timestamp_same_hash_is_noop(self):
        profile = {"enabled": True}
        envelope = {"schema": BACKUP_PROFILE_SYNC_SCHEMA, "version": 1, "profile_updated_at": 20, "profile": profile, "profile_sha256": backup_profile_sync_hash(profile)}
        with patch("mcd_agent.backup._backup_profile_effective_values", return_value=profile), patch("mcd_agent.backup.backup_profile_get", return_value={"_profile_updated_at": 20}), patch("mcd_agent.backup.backup_profile_for_push", return_value=profile), patch("mcd_agent.backup.backup_profile_set") as apply:
            _, result = backup_profile_apply_sync(object(), envelope)
        self.assertEqual(result["status"], "ok")
        apply.assert_not_called()

    def test_equal_timestamp_different_hash_is_conflict(self):
        profile = {"enabled": True}
        envelope = {"schema": BACKUP_PROFILE_SYNC_SCHEMA, "version": 1, "profile_updated_at": 20, "profile": profile, "profile_sha256": backup_profile_sync_hash(profile)}
        with patch("mcd_agent.backup._backup_profile_effective_values", return_value={"enabled": False}), patch("mcd_agent.backup.backup_profile_get", return_value={"_profile_updated_at": 20}), patch("mcd_agent.backup.backup_profile_for_push", return_value={"enabled": False}):
            _, result = backup_profile_apply_sync(object(), envelope)
        self.assertEqual(result["reason"], "profile_conflict")


if __name__ == "__main__":
    unittest.main()
