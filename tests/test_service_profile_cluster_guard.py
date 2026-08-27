from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import types
import unittest
from unittest.mock import Mock

if "pymysql" not in sys.modules:
    pymysql = types.ModuleType("pymysql")
    pymysql.connect = lambda **_kwargs: None
    pymysql.connections = types.SimpleNamespace(Connection=object)
    cursors = types.ModuleType("pymysql.cursors")
    cursors.DictCursor = object
    pymysql.cursors = cursors
    sys.modules["pymysql"] = pymysql
    sys.modules["pymysql.cursors"] = cursors

import mcd_agent.service_profiles as service_profiles


class ServiceProfileClusterGuardTests(unittest.TestCase):
    def test_mysql_override_writes_hardware_user_connection_limit(self) -> None:
        content = service_profiles._build_mysql_override(
            {
                "innodb_buffer_pool_size_mb": 32768,
                "max_connections": 600,
                "max_user_connections": 400,
            },
            engine="mysql",
        )

        self.assertIn("max_connections = 600", content)
        self.assertIn("max_user_connections = 400", content)

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

    def test_php_cli_opcache_override_keeps_runtime_defaults(self) -> None:
        content = service_profiles._build_cli_opcache_override(
            {
                "opcache_memory_mb": 384,
                "php": {
                    "realpath_cache_size_kb": 65536,
                    "realpath_cache_ttl_sec": 900,
                },
            }
        )

        self.assertIn("php-cli hardware-safe tuning", content)
        self.assertIn("opcache.memory_consumption=384", content)
        self.assertIn("realpath_cache_size=65536K", content)
        self.assertIn("realpath_cache_ttl=900", content)
        forbidden = (
            "memory_limit",
            "max_execution_time",
            "max_input_vars",
            "post_max_size",
            "upload_max_filesize",
            "session.",
            "date.timezone",
        )
        for key in forbidden:
            self.assertNotIn(key, content)

    def test_php_profile_apply_writes_cli_safe_opcache_dropin(self) -> None:
        cfg = SimpleNamespace(cluster_id="")
        old_euid = service_profiles.os.geteuid
        old_detect_php = service_profiles._detect_php_version
        old_find_php_fpm = service_profiles._find_php_fpm_bin
        old_detect_service = service_profiles._detect_php_fpm_service_name
        old_remove_legacy = service_profiles._remove_legacy_php_ini_baseline_files
        old_write = service_profiles._write_file
        old_run = service_profiles.subprocess.run
        writes: dict[str, str] = {}

        try:
            service_profiles.os.geteuid = lambda: 0
            service_profiles._detect_php_version = lambda: "8.3"
            service_profiles._find_php_fpm_bin = lambda _ver: "php-fpm8.3"
            service_profiles._detect_php_fpm_service_name = lambda _ver: "php8.3-fpm"
            service_profiles._remove_legacy_php_ini_baseline_files = lambda: []
            service_profiles._write_file = lambda path, content: writes.setdefault(str(path), content) is not None
            service_profiles.subprocess.run = lambda *a, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")

            res = service_profiles.apply_php_fpm_profile(
                cfg,
                {
                    "opcache_memory_mb": 384,
                    "php": {
                        "realpath_cache_size_kb": 65536,
                        "realpath_cache_ttl_sec": 900,
                    },
                },
                dry_run=False,
            )
        finally:
            service_profiles.os.geteuid = old_euid
            service_profiles._detect_php_version = old_detect_php
            service_profiles._find_php_fpm_bin = old_find_php_fpm
            service_profiles._detect_php_fpm_service_name = old_detect_service
            service_profiles._remove_legacy_php_ini_baseline_files = old_remove_legacy
            service_profiles._write_file = old_write
            service_profiles.subprocess.run = old_run

        self.assertEqual(res.get("status"), "applied")
        cli_content = writes["/etc/php/8.3/cli/conf.d/99-mcd-hw.ini"]
        fpm_content = writes["/etc/php/8.3/fpm/conf.d/99-mcd-hw.ini"]
        self.assertIn("php-cli hardware-safe tuning", cli_content)
        self.assertIn("php-fpm hardware tuning", fpm_content)
        self.assertNotIn("memory_limit", cli_content)
        self.assertNotEqual(cli_content, fpm_content)

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

    def test_cluster_mysql_profile_raises_connection_floor(self) -> None:
        sanitized = service_profiles._sanitize_cluster_mysql_profile(
            {
                "scope": "pxc",
                "cluster_safe": True,
                "max_connections": 600,
                "thread_cache_size": 128,
                "open_files_limit": 65535,
                "table_open_cache": 8000,
            }
        )

        self.assertEqual(sanitized["max_connections"], 2000)
        self.assertEqual(sanitized["thread_cache_size"], 256)
        self.assertEqual(sanitized["open_files_limit"], 262144)
        self.assertEqual(sanitized["table_open_cache"], 8000)


if __name__ == "__main__":
    unittest.main()
