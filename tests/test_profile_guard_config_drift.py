from __future__ import annotations

import hashlib
import json
import unittest
from unittest.mock import patch

from mcd_agent import config
from mcd_agent.config import _DEFAULT_SQL_SEGMENTS_DUE


def _old_segments_due_without_dnc_tracking() -> str:
    return _DEFAULT_SQL_SEGMENTS_DUE.replace(
        "  OR EXISTS (    SELECT 1     FROM {prefix}lead_donotcontact dnc     WHERE ll.filters LIKE '%dnc_%'       AND (ll.last_built_date IS NULL OR dnc.date_added > ll.last_built_date)     LIMIT 1  )",
        "",
    )


class ProfileGuardConfigDriftTests(unittest.TestCase):
    def test_legacy_desired_sql_default_migration_is_not_config_drift(self) -> None:
        old_segments_due = _old_segments_due_without_dnc_tracking()
        desired_text = "\n".join(
            [
                "[profile]",
                'name = "mini"',
                "[sql]",
                f"segments_due = {json.dumps(old_segments_due)}",
                "",
            ]
        )
        current_text = desired_text.replace(
            json.dumps(old_segments_due),
            json.dumps(_DEFAULT_SQL_SEGMENTS_DUE),
        )

        payload = {
            "status": "ok",
            "desired_profile": "mini",
            "config_source": "desired",
            "desired_config_toml": desired_text,
            "desired_config_sha256": hashlib.sha256(desired_text.encode("utf-8")).hexdigest(),
        }
        with patch.object(config, "_fetch_desired_config_payload_from_mcc", return_value=(True, payload)):
            drift = config.check_profile_drift_with_mcc(
                "/does/not/matter.toml",
                current_profile="mini",
                current_config_sha=hashlib.sha256(current_text.encode("utf-8")).hexdigest(),
            )

        self.assertEqual(drift["status"], "ok")
        self.assertEqual(drift.get("config_sha_mismatch_ignored"), "local_auto_migration")

    def test_real_desired_config_sha_mismatch_is_still_drift(self) -> None:
        desired_text = "[profile]\nname = \"mini\"\n[sql]\nsegments_due = \"SELECT 1\"\n"
        current_text = "[profile]\nname = \"mini\"\n[sql]\nsegments_due = \"SELECT 2\"\n"
        payload = {
            "status": "ok",
            "desired_profile": "mini",
            "config_source": "desired",
            "desired_config_toml": desired_text,
            "desired_config_sha256": hashlib.sha256(desired_text.encode("utf-8")).hexdigest(),
        }

        with patch.object(config, "_fetch_desired_config_payload_from_mcc", return_value=(True, payload)):
            drift = config.check_profile_drift_with_mcc(
                "/does/not/matter.toml",
                current_profile="mini",
                current_config_sha=hashlib.sha256(current_text.encode("utf-8")).hexdigest(),
            )

        self.assertEqual(drift["status"], "drift")
        self.assertEqual(drift["reason"], "config_sha_mismatch")


if __name__ == "__main__":
    unittest.main()
