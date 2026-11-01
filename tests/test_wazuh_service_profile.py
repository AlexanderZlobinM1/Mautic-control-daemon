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
import mcd_agent.apt_profile as apt_profile
import mcd_agent.service_profiles as service_profiles


class WazuhServiceProfileTests(unittest.TestCase):
    def test_legacy_component_defaults_backfill_wazuh(self) -> None:
        components = agent_config._normalize_service_profile_components(
            ["php_fpm", "mysql", "apt", "mautic_db_indexes"]
        )

        self.assertIn("wazuh", components)

    def test_apt_state_includes_wazuh_agent_state(self) -> None:
        old_pending = apt_profile._pending_updates
        old_duplicates = apt_profile.detect_duplicate_list_sources
        old_zabbix = apt_profile.collect_zabbix_mysql_monitor_state
        old_zabbix_agent = apt_profile.collect_zabbix_agent_state
        old_repo_profiles = apt_profile.collect_apt_repo_profiles_state
        old_unattended = apt_profile.collect_unattended_upgrade_state
        old_wazuh = apt_profile._collect_wazuh_agent_state_for_push
        try:
            apt_profile._pending_updates = lambda timeout_sec: (
                {
                    "pending_updates": 0,
                    "pending_total": 0,
                    "pending_phasing": 0,
                    "pending_hold": 0,
                    "upgradable_packages": [],
                    "phasing_packages": [],
                    "held_packages": [],
                },
                [],
            )
            apt_profile.detect_duplicate_list_sources = lambda: {"count": 0, "items": []}
            apt_profile.collect_zabbix_mysql_monitor_state = lambda cfg=None: {"status": "ok"}
            apt_profile.collect_zabbix_agent_state = lambda profile=None: {"status": "disabled"}
            apt_profile.collect_apt_repo_profiles_state = lambda cfg=None: {}
            apt_profile.collect_unattended_upgrade_state = lambda: {"status": "ok"}
            apt_profile._collect_wazuh_agent_state_for_push = lambda: {
                "installed": True,
                "version": "4.12.0-1",
                "service": {"active": True, "enabled": True},
            }

            state = apt_profile.collect_apt_state(auto_bootstrap_zabbix=False)
        finally:
            apt_profile._pending_updates = old_pending
            apt_profile.detect_duplicate_list_sources = old_duplicates
            apt_profile.collect_zabbix_mysql_monitor_state = old_zabbix
            apt_profile.collect_zabbix_agent_state = old_zabbix_agent
            apt_profile.collect_apt_repo_profiles_state = old_repo_profiles
            apt_profile.collect_unattended_upgrade_state = old_unattended
            apt_profile._collect_wazuh_agent_state_for_push = old_wazuh

        self.assertEqual(state.get("status"), "ok")
        self.assertEqual(state.get("wazuh_agent", {}).get("version"), "4.12.0-1")
        self.assertTrue(state.get("wazuh_agent", {}).get("installed"))

    def test_wazuh_apply_once_returns_ok_for_applied_profile(self) -> None:
        old_fetch = service_profiles.fetch_service_profile
        old_apply = service_profiles.apply_external_service_profile
        try:
            service_profiles.fetch_service_profile = lambda _cfg, _comp: {
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
            service_profiles.fetch_service_profile = lambda _cfg, _comp: {
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
