from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import mcd_agent.service_profiles as service_profiles


class ServiceProfileClusterGuardTests(unittest.TestCase):
    def test_php_fpm_opcache_override_includes_realpath_cache(self) -> None:
        content = service_profiles._build_opcache_override(
            {
                "opcache_memory_mb": 384,
                "php": {
                    "realpath_cache_size_kb": 65536,
                    "realpath_cache_ttl_sec": 900,
                },
            }
        )

        self.assertIn("realpath_cache_size=65536K", content)
        self.assertIn("realpath_cache_ttl=900", content)

    def test_cluster_db_indexes_skip_without_explicit_maintenance_flag(self) -> None:
        cfg = SimpleNamespace(cluster_id="ananasrs-prod")

        res = service_profiles.service_profiles_apply_once(
            cfg,
            component="mautic_db_indexes",
            dry_run=False,
        )

        self.assertEqual(res.get("status"), "skipped")
        self.assertEqual(res.get("reason"), "cluster_db_maintenance_requires_explicit_operator_flag")
        self.assertEqual(res.get("component"), "mautic_db_indexes")

    def test_cluster_mysql_skip_without_explicit_maintenance_flag(self) -> None:
        cfg = SimpleNamespace(cluster_id="ananasrs-prod")

        res = service_profiles.service_profiles_apply_once(
            cfg,
            component="mysql",
            dry_run=False,
        )

        self.assertEqual(res.get("status"), "skipped")
        self.assertEqual(res.get("reason"), "cluster_db_maintenance_requires_explicit_operator_flag")
        self.assertEqual(res.get("component"), "mysql")

    def test_cluster_db_indexes_manual_maintenance_flag_allows_apply(self) -> None:
        cfg = SimpleNamespace(cluster_id="ananasrs-prod")
        old_apply = service_profiles.apply_mautic_db_indexes
        try:
            service_profiles.apply_mautic_db_indexes = lambda _cfg, dry_run=False: {
                "status": "planned" if dry_run else "noop"
            }
            res = service_profiles.service_profiles_apply_once(
                cfg,
                component="mautic_db_indexes",
                dry_run=True,
                allow_cluster_db_maintenance=True,
            )
        finally:
            service_profiles.apply_mautic_db_indexes = old_apply

        self.assertEqual(res.get("status"), "ok")
        self.assertEqual(res.get("apply", {}).get("status"), "planned")

    def test_single_host_db_indexes_still_apply_without_maintenance_flag(self) -> None:
        cfg = SimpleNamespace(cluster_id="")
        old_detect = service_profiles._mysql_galera_config_detected
        old_apply = service_profiles.apply_mautic_db_indexes
        try:
            service_profiles._mysql_galera_config_detected = lambda: False
            service_profiles.apply_mautic_db_indexes = lambda _cfg, dry_run=False: {
                "status": "planned" if dry_run else "noop"
            }
            res = service_profiles.service_profiles_apply_once(
                cfg,
                component="mautic_db_indexes",
                dry_run=True,
            )
        finally:
            service_profiles._mysql_galera_config_detected = old_detect
            service_profiles.apply_mautic_db_indexes = old_apply

        self.assertEqual(res.get("status"), "ok")
        self.assertEqual(res.get("apply", {}).get("status"), "planned")

    def test_cluster_mysql_profile_does_not_cleanup_top_level_configs(self) -> None:
        cfg = SimpleNamespace(cluster_id="ananasrs-prod")
        old_euid = service_profiles.os.geteuid
        old_detect_service = service_profiles._detect_mysql_service_name
        old_detect_engine = service_profiles._detect_mysql_engine
        old_detect_galera = service_profiles._mysql_galera_config_detected
        old_detect_dropin = service_profiles._detect_mysql_dropin
        old_build = service_profiles._build_mysql_override
        old_write = service_profiles._write_file
        old_dynamic = service_profiles._apply_mysql_dynamic_profile
        old_cleanup_legacy = service_profiles._cleanup_legacy_mysql_top_level_configs
        old_cleanup_profile = service_profiles._cleanup_mysql_top_level_profile_overrides
        old_run = service_profiles.subprocess.run

        with TemporaryDirectory() as tmp:
            dropin = Path(tmp) / "99-mcd.cnf"
            cleanup_legacy = Mock(return_value={})
            cleanup_profile = Mock(return_value={})
            try:
                service_profiles.os.geteuid = lambda: 0
                service_profiles._detect_mysql_service_name = lambda: "mysql"
                service_profiles._detect_mysql_engine = lambda: "mysql"
                service_profiles._mysql_galera_config_detected = lambda: True
                service_profiles._detect_mysql_dropin = lambda _profile: dropin
                service_profiles._build_mysql_override = lambda _profile, engine="mysql": "[mysqld]\n"
                service_profiles._write_file = lambda _path, _content: True
                service_profiles._apply_mysql_dynamic_profile = lambda _profile, engine="mysql": {"status": "skipped"}
                service_profiles._cleanup_legacy_mysql_top_level_configs = cleanup_legacy
                service_profiles._cleanup_mysql_top_level_profile_overrides = cleanup_profile
                service_profiles.subprocess.run = lambda *a, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")

                res = service_profiles.apply_mysql_profile(cfg, {"cluster_safe": True}, dry_run=False)
            finally:
                service_profiles.os.geteuid = old_euid
                service_profiles._detect_mysql_service_name = old_detect_service
                service_profiles._detect_mysql_engine = old_detect_engine
                service_profiles._mysql_galera_config_detected = old_detect_galera
                service_profiles._detect_mysql_dropin = old_detect_dropin
                service_profiles._build_mysql_override = old_build
                service_profiles._write_file = old_write
                service_profiles._apply_mysql_dynamic_profile = old_dynamic
                service_profiles._cleanup_legacy_mysql_top_level_configs = old_cleanup_legacy
                service_profiles._cleanup_mysql_top_level_profile_overrides = old_cleanup_profile
                service_profiles.subprocess.run = old_run

        self.assertEqual(res.get("status"), "applied")
        cleanup_legacy.assert_not_called()
        cleanup_profile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
