from __future__ import annotations

import sys
from types import SimpleNamespace
import types
import unittest

if "pymysql" not in sys.modules:
    pymysql = types.ModuleType("pymysql")
    pymysql.connect = lambda **_kwargs: None
    pymysql.connections = types.SimpleNamespace(Connection=object)
    cursors = types.ModuleType("pymysql.cursors")
    cursors.DictCursor = object
    pymysql.cursors = cursors
    sys.modules["pymysql"] = pymysql
    sys.modules["pymysql.cursors"] = cursors

import mcd_agent.config as agent_config
import mcd_agent.service_profiles as service_profiles


class WazuhServiceProfileTests(unittest.TestCase):
    def test_legacy_component_defaults_backfill_wazuh(self) -> None:
        components = agent_config._normalize_service_profile_components(
            ["php_fpm", "mysql", "apt", "mautic_db_indexes"]
        )

        self.assertIn("wazuh", components)

    def test_wazuh_apply_once_returns_ok_for_applied_profile(self) -> None:
        old_fetch = service_profiles.fetch_service_profile
        old_apply = service_profiles.apply_external_service_profile
        try:
            service_profiles.fetch_service_profile = lambda _cfg, _comp, **_kwargs: {
                "status": "ok",
                "profile": {
                    "enabled": True,
                    "manager_address": "10.0.0.10",
                },
            }
            service_profiles.apply_external_service_profile = lambda _cfg, _fetched, *, component, dry_run=False: {
                "status": "applied",
                "dry_run": bool(dry_run),
            }
            res = service_profiles.service_profiles_apply_once(
                SimpleNamespace(cluster_id=""),
                component="wazuh",
                dry_run=False,
            )
        finally:
            service_profiles.fetch_service_profile = old_fetch
            service_profiles.apply_external_service_profile = old_apply

        self.assertEqual(res.get("status"), "ok")
        self.assertEqual(res.get("apply", {}).get("status"), "applied")

    def test_wazuh_apply_once_returns_error_for_failed_profile(self) -> None:
        old_fetch = service_profiles.fetch_service_profile
        old_apply = service_profiles.apply_external_service_profile
        try:
            service_profiles.fetch_service_profile = lambda _cfg, _comp, **_kwargs: {
                "status": "ok",
                "profile": {
                    "enabled": True,
                    "manager_address": "10.0.0.10",
                },
            }
            service_profiles.apply_external_service_profile = lambda _cfg, _fetched, *, component, dry_run=False: {
                "status": "error",
                "reason": "manager_unreachable",
            }
            res = service_profiles.service_profiles_apply_once(
                SimpleNamespace(cluster_id=""),
                component="wazuh",
                dry_run=False,
            )
        finally:
            service_profiles.fetch_service_profile = old_fetch
            service_profiles.apply_external_service_profile = old_apply

        self.assertEqual(res.get("status"), "error")
        self.assertEqual(res.get("reason"), "manager_unreachable")





if __name__ == "__main__":
    unittest.main()
