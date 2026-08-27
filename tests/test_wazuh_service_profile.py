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
import mcd_agent.wazuh_profile as wazuh_profile


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
        old_apply = service_profiles.apply_wazuh_profile
        try:
            service_profiles.fetch_service_profile = lambda _cfg, _comp: {
                "status": "ok",
                "profile": {
                    "enabled": True,
                    "manager_address": "10.0.0.10",
                },
            }
            service_profiles.apply_wazuh_profile = lambda _profile, _cfg, dry_run=False: {
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
            service_profiles.apply_wazuh_profile = old_apply

        self.assertEqual(res.get("status"), "ok")
        self.assertEqual(res.get("apply", {}).get("status"), "applied")

    def test_wazuh_apply_once_returns_error_for_failed_profile(self) -> None:
        old_fetch = service_profiles.fetch_service_profile
        old_apply = service_profiles.apply_wazuh_profile
        try:
            service_profiles.fetch_service_profile = lambda _cfg, _comp: {
                "status": "ok",
                "profile": {
                    "enabled": True,
                    "manager_address": "10.0.0.10",
                },
            }
            service_profiles.apply_wazuh_profile = lambda _profile, _cfg, dry_run=False: {
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
            service_profiles.apply_wazuh_profile = old_apply

        self.assertEqual(res.get("status"), "error")
        self.assertEqual(res.get("reason"), "manager_unreachable")

    def test_wazuh_profile_dry_run_stays_planned_without_root(self) -> None:
        old_collect = wazuh_profile.collect_wazuh_agent_state
        try:
            wazuh_profile.collect_wazuh_agent_state = lambda _profile=None: {"installed": False}
            res = wazuh_profile.apply_wazuh_profile({"enabled": True}, SimpleNamespace(), dry_run=True)
        finally:
            wazuh_profile.collect_wazuh_agent_state = old_collect

        self.assertEqual(res.get("status"), "planned")
        self.assertIn("wazuh_agent_install", res.get("actions", []))

    def test_existing_wazuh_agent_still_reconciles_repo_without_reinstall(self) -> None:
        old_collect = wazuh_profile.collect_wazuh_agent_state
        old_geteuid = wazuh_profile.os.geteuid
        old_prereq = wazuh_profile._ensure_prerequisites
        old_repo = wazuh_profile._ensure_wazuh_repo
        old_keyring = wazuh_profile._ensure_wazuh_keyring
        old_apt_update = wazuh_profile._apt_update
        old_pkg_state = wazuh_profile._pkg_state
        old_install = wazuh_profile._install_wazuh_agent
        old_config = wazuh_profile._ensure_agent_config
        old_hold = wazuh_profile._set_package_hold
        old_run = wazuh_profile._run
        calls: list[str] = []
        try:
            wazuh_profile.collect_wazuh_agent_state = lambda _profile=None: {"installed": True}
            wazuh_profile.os.geteuid = lambda: 0
            wazuh_profile._ensure_prerequisites = lambda _timeout_sec: calls.append("prerequisites")
            wazuh_profile._ensure_wazuh_repo = lambda _profile: calls.append("repo") or True
            wazuh_profile._ensure_wazuh_keyring = lambda _profile, *, timeout_sec: (
                calls.append("keyring") or (True, "/usr/share/keyrings/wazuh.gpg")
            )
            wazuh_profile._apt_update = lambda _timeout_sec: calls.append("apt_update")
            wazuh_profile._pkg_state = lambda _package: (True, "4.12.0")
            wazuh_profile._install_wazuh_agent = lambda _profile, _cfg, *, timeout_sec: calls.append("install")
            wazuh_profile._ensure_agent_config = lambda _profile, _cfg: (
                calls.append("config") or (False, [])
            )
            wazuh_profile._set_package_hold = lambda _hold: calls.append("hold") or True
            wazuh_profile._run = lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr="")

            res = wazuh_profile.apply_wazuh_profile(
                {
                    "enabled": True,
                    "manager_address": "10.0.0.10",
                    "repo_enabled": True,
                    "ensure_prerequisites": True,
                    "install_package": True,
                    "start_service": False,
                },
                SimpleNamespace(),
            )
        finally:
            wazuh_profile.collect_wazuh_agent_state = old_collect
            wazuh_profile.os.geteuid = old_geteuid
            wazuh_profile._ensure_prerequisites = old_prereq
            wazuh_profile._ensure_wazuh_repo = old_repo
            wazuh_profile._ensure_wazuh_keyring = old_keyring
            wazuh_profile._apt_update = old_apt_update
            wazuh_profile._pkg_state = old_pkg_state
            wazuh_profile._install_wazuh_agent = old_install
            wazuh_profile._ensure_agent_config = old_config
            wazuh_profile._set_package_hold = old_hold
            wazuh_profile._run = old_run

        self.assertEqual(res.get("status"), "applied")
        self.assertIn("repo", calls)
        self.assertIn("keyring", calls)
        self.assertIn("apt_update", calls)
        self.assertNotIn("install", calls)

    def test_existing_wazuh_agent_upgrades_when_package_update_available(self) -> None:
        old_collect = wazuh_profile.collect_wazuh_agent_state
        old_geteuid = wazuh_profile.os.geteuid
        old_prereq = wazuh_profile._ensure_prerequisites
        old_repo = wazuh_profile._ensure_wazuh_repo
        old_keyring = wazuh_profile._ensure_wazuh_keyring
        old_pkg_state = wazuh_profile._pkg_state
        old_install = wazuh_profile._install_wazuh_agent
        old_upgrade_available = wazuh_profile._wazuh_upgrade_available
        old_upgrade = wazuh_profile._upgrade_wazuh_agent
        old_config = wazuh_profile._ensure_agent_config
        old_hold = wazuh_profile._set_package_hold
        old_run = wazuh_profile._run
        calls: list[str] = []
        try:
            wazuh_profile.collect_wazuh_agent_state = lambda _profile=None: {"installed": True}
            wazuh_profile.os.geteuid = lambda: 0
            wazuh_profile._ensure_prerequisites = lambda _timeout_sec: calls.append("prerequisites")
            wazuh_profile._ensure_wazuh_repo = lambda _profile: calls.append("repo") or False
            wazuh_profile._ensure_wazuh_keyring = lambda _profile, *, timeout_sec: (
                calls.append("keyring") or (False, "/usr/share/keyrings/wazuh.gpg")
            )
            wazuh_profile._pkg_state = lambda _package: (True, "4.14.3-1")
            wazuh_profile._install_wazuh_agent = lambda _profile, _cfg, *, timeout_sec: calls.append("install")
            wazuh_profile._wazuh_upgrade_available = lambda _timeout_sec: calls.append("upgrade_check") or True
            wazuh_profile._upgrade_wazuh_agent = lambda _profile, _cfg, *, timeout_sec: calls.append("upgrade")
            wazuh_profile._ensure_agent_config = lambda _profile, _cfg: (
                calls.append("config") or (False, [])
            )
            wazuh_profile._set_package_hold = lambda _hold: calls.append("hold") or True
            wazuh_profile._run = lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr="")

            res = wazuh_profile.apply_wazuh_profile(
                {
                    "enabled": True,
                    "manager_address": "10.0.0.10",
                    "repo_enabled": True,
                    "ensure_prerequisites": True,
                    "install_package": True,
                    "start_service": False,
                    "hold_package": False,
                },
                SimpleNamespace(),
            )
        finally:
            wazuh_profile.collect_wazuh_agent_state = old_collect
            wazuh_profile.os.geteuid = old_geteuid
            wazuh_profile._ensure_prerequisites = old_prereq
            wazuh_profile._ensure_wazuh_repo = old_repo
            wazuh_profile._ensure_wazuh_keyring = old_keyring
            wazuh_profile._pkg_state = old_pkg_state
            wazuh_profile._install_wazuh_agent = old_install
            wazuh_profile._wazuh_upgrade_available = old_upgrade_available
            wazuh_profile._upgrade_wazuh_agent = old_upgrade
            wazuh_profile._ensure_agent_config = old_config
            wazuh_profile._set_package_hold = old_hold
            wazuh_profile._run = old_run

        self.assertEqual(res.get("status"), "applied")
        self.assertIn("upgrade_check", calls)
        self.assertIn("upgrade", calls)
        self.assertNotIn("install", calls)
        self.assertIn("wazuh_agent:upgraded", res.get("actions", []))


if __name__ == "__main__":
    unittest.main()
