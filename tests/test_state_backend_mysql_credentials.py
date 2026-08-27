from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcd_agent.cli import _state_backend_config_and_status, _state_db_missing_only
from mcd_agent.state_backend import mysql_state_enabled, state_backend_status


def _cfg(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "state_backend": "mysql_hybrid",
        "state_mysql_host": "localhost",
        "state_mysql_unix_socket": "",
        "state_mysql_port": 3306,
        "state_mysql_database": "mcd_state",
        "state_mysql_user": "root",
        "state_mysql_password": "",
        "state_mysql_table_prefix": "mcd_",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class StateBackendMysqlCredentialTests(unittest.TestCase):
    def test_status_returns_effective_runtime_config_for_bootstrap(self) -> None:
        cfg = _cfg(state_backend="sqlite")
        cfg_eff = _cfg(state_mysql_user="mcd_state", state_mysql_password="secret")
        status = {
            "mode": "legacy",
            "active_backend": "sqlite",
            "reason": "mysql_schema_unavailable",
        }

        with (
            patch("mcd_agent.cli.fetch_runtime_overrides", return_value={"status": "ok", "runtime_overrides": {"state_backend": "mysql_hybrid"}}),
            patch("mcd_agent.cli.apply_remote_overrides", return_value={"config": cfg_eff}),
            patch("mcd_agent.cli.state_backend_status", return_value=status) as backend_status,
        ):
            actual_cfg, actual_status = _state_backend_config_and_status(cfg)

        self.assertIs(actual_cfg, cfg_eff)
        self.assertEqual(actual_status, status)
        backend_status.assert_called_once_with(cfg_eff, probe=True)

    def test_root_without_password_falls_back_to_sqlite(self) -> None:
        cfg = _cfg()

        self.assertFalse(mysql_state_enabled(cfg))
        status = state_backend_status(cfg, probe=False)

        self.assertEqual(status["active_backend"], "sqlite")
        self.assertEqual(status["reason"], "mysql_root_without_password_disabled")

    def test_non_root_with_password_can_enable_mysql_state(self) -> None:
        cfg = _cfg(state_mysql_user="mcd_state", state_mysql_password="secret")

        self.assertTrue(mysql_state_enabled(cfg))

    def test_bootstrap_allows_existing_database_with_missing_schema(self) -> None:
        status = {
            "mode": "legacy",
            "active_backend": "sqlite",
            "reason": "mysql_schema_unavailable",
            "error": "state schema not initialized: table=mcd_schema_version missing=id,schema_version,updated_at",
        }

        with patch("mcd_agent.cli.state_database_exists", return_value=(True, "ok")):
            self.assertTrue(_state_db_missing_only(_cfg(), status))

    def test_bootstrap_rejects_existing_database_for_unrelated_probe_error(self) -> None:
        status = {
            "mode": "legacy",
            "active_backend": "sqlite",
            "reason": "mysql_schema_unavailable",
            "error": "connection timed out",
        }

        with patch("mcd_agent.cli.state_database_exists", return_value=(True, "ok")):
            self.assertFalse(_state_db_missing_only(_cfg(), status))

    def test_bootstrap_rejects_active_mysql_backend(self) -> None:
        status = {
            "mode": "mysql",
            "active_backend": "mysql",
            "reason": "ok",
        }

        with patch("mcd_agent.cli.state_database_exists") as exists:
            self.assertFalse(_state_db_missing_only(_cfg(), status))

        exists.assert_not_called()


if __name__ == "__main__":
    unittest.main()
