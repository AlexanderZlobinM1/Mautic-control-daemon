from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcd_agent.backup import _cfg_with_profile_payload, backup_profile_get, backup_profile_set
from mcd_agent.config import load_config


def _cfg(tmp: Path, *, enabled: bool = True):
    path = tmp / "mcd.toml"
    path.write_text(
        "\n".join(
            [
                "[runtime]",
                f'state_db_path = "{tmp / "state.db"}"',
                "[backup]",
                f"enabled = {'true' if enabled else 'false'}",
                f'state_dir = "{tmp / "state"}"',
                f'lock_dir = "{tmp / "locks"}"',
                f'mount_base_dir = "{tmp / "mounts"}"',
                "[backup.secrets]",
                f'key_path = "{tmp / "keys" / "backup.key"}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return load_config(str(path), allow_recover_from_mcc=False)


class BackupProfilePrepareTests(unittest.TestCase):
    def test_profile_set_does_not_persist_when_prepare_check_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(Path(td))

            with patch("mcd_agent.backup._backup_profile_prepare_check", side_effect=RuntimeError("bad storage")):
                with self.assertRaisesRegex(RuntimeError, "bad storage"):
                    backup_profile_set(cfg, {"storage": {"host": "box", "user": "u"}}, prepare_check=True)

            self.assertEqual(backup_profile_get(cfg), {})

    def test_profile_set_returns_prepare_check_result_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(Path(td))
            check = {"status": "ok", "db_instances": 2}

            with patch("mcd_agent.backup._backup_profile_prepare_check", return_value=check):
                result = backup_profile_set(
                    cfg,
                    {"storage": {"host": "box", "user": "u", "password": "secret"}},
                    prepare_check=True,
                )

            self.assertEqual(result.get("_prepare_check"), check)
            persisted = backup_profile_get(cfg)
            self.assertEqual(persisted["storage"]["host"], "box")
            self.assertNotIn("_prepare_check", persisted)

    def test_profile_payload_can_enable_disabled_backup_for_operation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(Path(td), enabled=False)

            effective = _cfg_with_profile_payload(
                cfg,
                {
                    "enabled": True,
                    "method": "mydump",
                    "remote_root_dir": "/mcc/deleted-instances",
                    "retention_copies": 3,
                    "storage": {"host": "box", "user": "u", "password": "secret"},
                },
            )

            self.assertTrue(effective.backup_enabled)
            self.assertEqual(effective.backup_method, "mydumper")
            self.assertEqual(effective.backup_remote_root_dir, "mcc/deleted-instances")
            self.assertEqual(effective.backup_retention_copies, 3)

    def test_storage_only_profile_set_repairs_deleted_instances_remote_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(Path(td))

            with patch("mcd_agent.backup._backup_profile_prepare_check", return_value={"status": "ok"}):
                backup_profile_set(
                    cfg,
                    {
                        "storage": {"host": "box", "user": "u", "password": "secret"},
                        "remote_root_dir": "mcc/deleted-instances",
                    },
                    prepare_check=True,
                )
                backup_profile_set(
                    cfg,
                    {"storage": {"host": "box", "user": "u", "password": "secret"}},
                    prepare_check=True,
                )

            persisted = backup_profile_get(cfg)
            self.assertEqual(persisted["remote_root_dir"], "backup")


if __name__ == "__main__":
    unittest.main()
