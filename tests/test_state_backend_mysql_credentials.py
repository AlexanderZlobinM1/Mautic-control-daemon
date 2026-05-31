from __future__ import annotations

from types import SimpleNamespace
import unittest

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
    def test_root_without_password_falls_back_to_sqlite(self) -> None:
        cfg = _cfg()

        self.assertFalse(mysql_state_enabled(cfg))
        status = state_backend_status(cfg, probe=False)

        self.assertEqual(status["active_backend"], "sqlite")
        self.assertEqual(status["reason"], "mysql_root_without_password_disabled")

    def test_non_root_with_password_can_enable_mysql_state(self) -> None:
        cfg = _cfg(state_mysql_user="mcd_state", state_mysql_password="secret")

        self.assertTrue(mysql_state_enabled(cfg))


if __name__ == "__main__":
    unittest.main()

