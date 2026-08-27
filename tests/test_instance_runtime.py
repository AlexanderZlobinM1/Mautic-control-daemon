from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent import instance_runtime
from mcd_agent.models import MauticInstall


class InstanceRuntimeTest(unittest.TestCase):
    def _install(self, root: Path) -> MauticInstall:
        docroot = root / "docroot"
        docroot.mkdir(parents=True)
        return MauticInstall(
            instance_uid="merkurosiguranje.sales-snap.com",
            name="merkurosiguranje",
            root=str(root),
            console_path=str(root / "bin" / "console"),
            primary_domain="merkurosiguranje.sales-snap.com",
            mautic_timezone="Europe/Belgrade",
            domains=["merkurosiguranje.sales-snap.com"],
        )

    def _fake_run(self, cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    def test_keeps_shared_fpm_socket_and_removes_legacy_runtime_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            php_etc = base / "etc" / "php"
            generated = base / "opt" / "mcd" / "generated"
            backups = base / "backups"
            sites_available = base / "etc" / "nginx" / "sites-available"
            sites_enabled = base / "etc" / "nginx" / "sites-enabled"
            sites_available.mkdir(parents=True)
            sites_enabled.mkdir(parents=True)
            inst_root = base / "var" / "www" / "merkurosiguranje" / "public_html"
            inst = self._install(inst_root)
            legacy_dir = inst_root / ".mcd"
            legacy_dir.mkdir()
            (legacy_dir / "php").write_text("#!/bin/sh\n", encoding="utf-8")
            vhost = sites_available / "merkurosiguranje.sales-snap.com.conf"
            vhost.write_text(
                f"""
server {{
    server_name merkurosiguranje.sales-snap.com;
    root {inst_root / "docroot"};
    location ~ \\.php$ {{
        fastcgi_pass unix:/run/php/php8.3-fpm.sock;
    }}
}}
""",
                encoding="utf-8",
            )
            (sites_enabled / vhost.name).symlink_to(vhost)

            with (
                patch.object(instance_runtime, "PHP_ETC_ROOT", php_etc),
                patch.object(instance_runtime, "GENERATED_ROOT", generated),
                patch.object(instance_runtime, "BACKUP_ROOT", backups),
                patch.object(instance_runtime, "NGINX_SITES_AVAILABLE", sites_available),
                patch.object(instance_runtime, "NGINX_SITES_ENABLED", sites_enabled),
                patch.object(instance_runtime, "_run", self._fake_run),
            ):
                payload = instance_runtime.apply_instance_runtime([inst], reload_services=False)

            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["changed"])
            self.assertFalse((php_etc / "8.3" / "fpm" / "pool.d" / "99-mcd.conf").exists())
            self.assertFalse((php_etc / "8.3" / "fpm" / "pool.d" / "mcd").exists())
            self.assertFalse((generated / "php" / "8.3" / "fpm" / "pools" / "mcd-merkurosiguranje.conf").exists())
            self.assertIn("fastcgi_pass unix:/run/php/php8.3-fpm.sock;", vhost.read_text(encoding="utf-8"))
            self.assertFalse(legacy_dir.exists())
            self.assertIn(f"legacy_mcd_removed:{inst_root}", payload["actions"])

    def test_detects_legacy_var_run_php_fpm_socket(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            php_etc = base / "etc" / "php"
            generated = base / "opt" / "mcd" / "generated"
            backups = base / "backups"
            sites_available = base / "etc" / "nginx" / "sites-available"
            sites_enabled = base / "etc" / "nginx" / "sites-enabled"
            sites_available.mkdir(parents=True)
            sites_enabled.mkdir(parents=True)
            inst_root = base / "var" / "www" / "merkurosiguranje" / "public_html"
            inst = self._install(inst_root)
            vhost = sites_available / "merkurosiguranje.sales-snap.com.conf"
            vhost.write_text(
                f"""
server {{
    server_name merkurosiguranje.sales-snap.com;
    root {inst_root / "docroot"};
    location ~ \\.php$ {{
        fastcgi_pass unix:/var/run/php/php8.3-fpm.sock;
    }}
}}
""",
                encoding="utf-8",
            )
            (sites_enabled / vhost.name).symlink_to(vhost)

            with (
                patch.object(instance_runtime, "PHP_ETC_ROOT", php_etc),
                patch.object(instance_runtime, "GENERATED_ROOT", generated),
                patch.object(instance_runtime, "BACKUP_ROOT", backups),
                patch.object(instance_runtime, "NGINX_SITES_AVAILABLE", sites_available),
                patch.object(instance_runtime, "NGINX_SITES_ENABLED", sites_enabled),
                patch.object(instance_runtime, "_run", self._fake_run),
            ):
                payload = instance_runtime.apply_instance_runtime([inst], reload_services=False)

            self.assertEqual(payload["status"], "ok")
            self.assertFalse(payload["changed"])
            self.assertEqual(payload["instances"][0]["php_versions"], ["8.3"])
            self.assertIn("fastcgi_pass unix:/var/run/php/php8.3-fpm.sock;", vhost.read_text(encoding="utf-8"))
            self.assertFalse((inst_root / ".mcd").exists())

    def test_removes_orphan_mcd_dir_under_managed_web_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            web_root = base / "var" / "www"
            orphan = web_root / "retired-site" / "public_html" / ".mcd"
            orphan.mkdir(parents=True)
            (orphan / "php").write_text("#!/bin/sh\n", encoding="utf-8")
            backups = base / "backups"

            with (
                patch.object(instance_runtime, "BACKUP_ROOT", backups),
                patch.object(instance_runtime, "LEGACY_INSTANCE_SEARCH_ROOTS", (web_root,)),
                patch.object(instance_runtime, "_run", self._fake_run),
            ):
                payload = instance_runtime.apply_instance_runtime([], reload_services=False)

            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["changed"])
            self.assertFalse(orphan.exists())

    def test_idempotent_apply_does_not_create_another_backup_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            php_etc = base / "etc" / "php"
            generated = base / "opt" / "mcd" / "generated"
            backups = base / "backups"
            sites_available = base / "etc" / "nginx" / "sites-available"
            sites_enabled = base / "etc" / "nginx" / "sites-enabled"
            sites_available.mkdir(parents=True)
            sites_enabled.mkdir(parents=True)
            inst_root = base / "var" / "www" / "merkurosiguranje" / "public_html"
            inst = self._install(inst_root)
            vhost = sites_available / "merkurosiguranje.sales-snap.com.conf"
            vhost.write_text(
                f"""
server {{
    server_name merkurosiguranje.sales-snap.com;
    root {inst_root / "docroot"};
    location ~ \\.php$ {{
        fastcgi_pass unix:/run/php/php8.3-fpm-mcd-merkurosiguranje.sock;
    }}
}}
""",
                encoding="utf-8",
            )
            (sites_enabled / vhost.name).symlink_to(vhost)

            with (
                patch.object(instance_runtime, "PHP_ETC_ROOT", php_etc),
                patch.object(instance_runtime, "GENERATED_ROOT", generated),
                patch.object(instance_runtime, "BACKUP_ROOT", backups),
                patch.object(instance_runtime, "NGINX_SITES_AVAILABLE", sites_available),
                patch.object(instance_runtime, "NGINX_SITES_ENABLED", sites_enabled),
                patch.object(instance_runtime, "_run", self._fake_run),
            ):
                first = instance_runtime.apply_instance_runtime([inst], reload_services=False)
                snapshots_after_first = sorted(path.name for path in backups.iterdir())
                second = instance_runtime.apply_instance_runtime([inst], reload_services=False)
                snapshots_after_second = sorted(path.name for path in backups.iterdir())

            self.assertEqual(first["status"], "ok")
            self.assertTrue(first["changed"])
            self.assertEqual(len(snapshots_after_first), 1)
            self.assertEqual(second["status"], "ok")
            self.assertFalse(second["changed"])
            self.assertEqual(second["backup_dir"], "")
            self.assertEqual(snapshots_after_second, snapshots_after_first)

    def test_prunes_only_timestamped_runtime_backups(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            backups = base / "backups"
            backups.mkdir()
            for index in range(25):
                snapshot = backups / f"20260718T{index:06d}Z"
                snapshot.mkdir()
                (snapshot / "nginx.conf").write_text("snapshot\n", encoding="utf-8")
            manual = backups / "manual-keep"
            manual.mkdir()
            external = base / "external"
            external.mkdir()
            symlink = backups / "20260719T000000Z"
            symlink.symlink_to(external, target_is_directory=True)

            with patch.object(instance_runtime, "BACKUP_ROOT", backups):
                payload = instance_runtime.apply_instance_runtime([], reload_services=False)

            remaining = sorted(
                path.name
                for path in backups.iterdir()
                if path.is_dir() and not path.is_symlink() and path.name.startswith("2026")
            )
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["backup_pruned"], 5)
            self.assertEqual(len(remaining), 20)
            self.assertEqual(remaining[0], "20260718T000005Z")
            self.assertTrue(manual.is_dir())
            self.assertTrue(symlink.is_symlink())
            self.assertTrue(external.is_dir())

    def test_rewrites_generic_php_fpm_socket_to_single_active_shared_socket(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            php_etc = base / "etc" / "php"
            generated = base / "opt" / "mcd" / "generated"
            backups = base / "backups"
            sites_available = base / "etc" / "nginx" / "sites-available"
            sites_enabled = base / "etc" / "nginx" / "sites-enabled"
            sites_available.mkdir(parents=True)
            sites_enabled.mkdir(parents=True)
            inst_root = base / "var" / "www" / "merkurosiguranje" / "public_html"
            inst = self._install(inst_root)
            vhost = sites_available / "merkurosiguranje.sales-snap.com.conf"
            vhost.write_text(
                f"""
server {{
    server_name merkurosiguranje.sales-snap.com;
    root {inst_root / "docroot"};
    location ~ \\.php$ {{
        fastcgi_pass unix:/run/php/php-fpm.sock;
    }}
}}
""",
                encoding="utf-8",
            )
            (sites_enabled / vhost.name).symlink_to(vhost)

            with (
                patch.object(instance_runtime, "PHP_ETC_ROOT", php_etc),
                patch.object(instance_runtime, "GENERATED_ROOT", generated),
                patch.object(instance_runtime, "BACKUP_ROOT", backups),
                patch.object(instance_runtime, "NGINX_SITES_AVAILABLE", sites_available),
                patch.object(instance_runtime, "NGINX_SITES_ENABLED", sites_enabled),
                patch.object(instance_runtime, "_single_active_shared_php_version", return_value="8.4"),
                patch.object(instance_runtime, "_run", self._fake_run),
            ):
                payload = instance_runtime.apply_instance_runtime([inst], reload_services=False)

            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["changed"])
            self.assertEqual(payload["instances"][0]["php_versions"], ["8.4"])
            self.assertIn("nginx_shared:", " ".join(payload["actions"]))
            self.assertIn("fastcgi_pass unix:/run/php/php8.4-fpm.sock;", vhost.read_text(encoding="utf-8"))
            self.assertFalse((inst_root / ".mcd").exists())

    def test_ignores_inactive_backup_nginx_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            php_etc = base / "etc" / "php"
            generated = base / "opt" / "mcd" / "generated"
            backups = base / "backups"
            sites_available = base / "etc" / "nginx" / "sites-available"
            sites_enabled = base / "etc" / "nginx" / "sites-enabled"
            sites_available.mkdir(parents=True)
            sites_enabled.mkdir(parents=True)
            inst_root = base / "var" / "www" / "merkurosiguranje" / "public_html"
            inst = self._install(inst_root)
            backup_vhost = sites_available / "merkurosiguranje.sales-snap.com.conf.emmy-backup-20260628T120000Z.bak"
            original = f"""
server {{
    server_name merkurosiguranje.sales-snap.com;
    root {inst_root / "docroot"};
    location ~ \\.php$ {{
        fastcgi_pass unix:/run/php/php8.3-fpm.sock;
    }}
}}
"""
            backup_vhost.write_text(original, encoding="utf-8")

            with (
                patch.object(instance_runtime, "PHP_ETC_ROOT", php_etc),
                patch.object(instance_runtime, "GENERATED_ROOT", generated),
                patch.object(instance_runtime, "BACKUP_ROOT", backups),
                patch.object(instance_runtime, "NGINX_SITES_AVAILABLE", sites_available),
                patch.object(instance_runtime, "NGINX_SITES_ENABLED", sites_enabled),
                patch.object(instance_runtime, "_run", self._fake_run),
            ):
                payload = instance_runtime.apply_instance_runtime([inst], reload_services=False)

            self.assertEqual(payload["status"], "ok")
            self.assertFalse(payload["changed"])
            self.assertEqual(payload["instances"][0]["status"], "skipped")
            self.assertEqual(payload["instances"][0]["nginx_files"], [])
            self.assertEqual(backup_vhost.read_text(encoding="utf-8"), original)

    def test_removes_stale_pool_include_and_reloads_fpm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            php_etc = base / "etc" / "php"
            generated = base / "opt" / "mcd" / "generated"
            backups = base / "backups"
            sites_available = base / "etc" / "nginx" / "sites-available"
            sites_enabled = base / "etc" / "nginx" / "sites-enabled"
            sites_available.mkdir(parents=True)
            sites_enabled.mkdir(parents=True)
            inst_root = base / "var" / "www" / "merkurosiguranje" / "public_html"
            inst = self._install(inst_root)
            slug = "merkurosiguranje"
            pool_dir = generated / "php" / "8.3" / "fpm" / "pools"
            pool_dir.mkdir(parents=True)
            pool_file = pool_dir / f"mcd-{slug}.conf"
            pool_file.write_text("; Managed by MCD. Generated per-instance PHP-FPM pool.\n", encoding="utf-8")
            pool_d = php_etc / "8.3" / "fpm" / "pool.d"
            pool_d.mkdir(parents=True)
            include_file = pool_d / "99-mcd.conf"
            include_file.write_text(
                "; Managed by MCD. Includes generated per-instance FPM pools.\n"
                "include=/etc/php/8.3/fpm/pool.d/mcd/*.conf\n",
                encoding="utf-8",
            )
            pool_link = pool_d / "mcd"
            pool_link.symlink_to(pool_dir)
            wrapper = generated / "instances" / slug / "php"
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
            instance_wrapper = inst_root / ".mcd" / "php"
            instance_wrapper.parent.mkdir(parents=True)
            instance_wrapper.symlink_to(wrapper)
            vhost = sites_available / "merkurosiguranje.sales-snap.com.conf"
            vhost.write_text(
                f"""
server {{
    server_name merkurosiguranje.sales-snap.com;
    root {inst_root / "docroot"};
    location ~ \\.php$ {{
        fastcgi_pass unix:/run/php/php8.3-fpm-mcd-merkurosiguranje.sock;
    }}
}}
""",
                encoding="utf-8",
            )
            (sites_enabled / vhost.name).symlink_to(vhost)
            calls: list[list[str]] = []

            def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(cmd)
                if cmd[:2] == ["systemctl", "is-active"]:
                    return subprocess.CompletedProcess(cmd, 0, "active\n", "")
                return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

            with (
                patch.object(instance_runtime, "PHP_ETC_ROOT", php_etc),
                patch.object(instance_runtime, "GENERATED_ROOT", generated),
                patch.object(instance_runtime, "BACKUP_ROOT", backups),
                patch.object(instance_runtime, "NGINX_SITES_AVAILABLE", sites_available),
                patch.object(instance_runtime, "NGINX_SITES_ENABLED", sites_enabled),
                patch.object(instance_runtime, "_run", fake_run),
            ):
                payload = instance_runtime.apply_instance_runtime([inst], reload_services=True)

            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["changed"])
            self.assertIn("pool_removed:merkurosiguranje:8.3", payload["actions"])
            self.assertIn("fpm_include_removed:8.3", payload["actions"])
            self.assertIn("fpm_link_removed:8.3", payload["actions"])
            self.assertFalse(pool_file.exists())
            self.assertFalse(include_file.exists())
            self.assertFalse(pool_link.exists())
            self.assertIn("fastcgi_pass unix:/run/php/php8.3-fpm.sock;", vhost.read_text(encoding="utf-8"))
            self.assertIn(["systemctl", "reload", "php8.3-fpm"], calls)
            self.assertIn(["systemctl", "reload", "nginx"], calls)

    def test_keeps_pool_include_when_other_mcd_vhost_remains(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            php_etc = base / "etc" / "php"
            generated = base / "opt" / "mcd" / "generated"
            backups = base / "backups"
            sites_available = base / "etc" / "nginx" / "sites-available"
            sites_enabled = base / "etc" / "nginx" / "sites-enabled"
            sites_available.mkdir(parents=True)
            sites_enabled.mkdir(parents=True)
            inst_root = base / "var" / "www" / "merkurosiguranje" / "public_html"
            other_root = base / "var" / "www" / "other" / "public_html"
            other_root.joinpath("docroot").mkdir(parents=True)
            inst = self._install(inst_root)
            pool_dir = generated / "php" / "8.3" / "fpm" / "pools"
            pool_dir.mkdir(parents=True)
            pool_d = php_etc / "8.3" / "fpm" / "pool.d"
            pool_d.mkdir(parents=True)
            include_file = pool_d / "99-mcd.conf"
            include_file.write_text(
                "; Managed by MCD. Includes generated per-instance FPM pools.\n"
                "include=/etc/php/8.3/fpm/pool.d/mcd/*.conf\n",
                encoding="utf-8",
            )
            pool_link = pool_d / "mcd"
            pool_link.symlink_to(pool_dir)
            vhost = sites_available / "merkurosiguranje.sales-snap.com.conf"
            vhost.write_text(
                f"""
server {{
    server_name merkurosiguranje.sales-snap.com;
    root {inst_root / "docroot"};
    location ~ \\.php$ {{
        fastcgi_pass unix:/run/php/php8.3-fpm-mcd-merkurosiguranje.sock;
    }}
}}
""",
                encoding="utf-8",
            )
            other_vhost = sites_available / "other.sales-snap.com.conf"
            other_vhost.write_text(
                f"""
server {{
    server_name other.sales-snap.com;
    root {other_root / "docroot"};
    location ~ \\.php$ {{
        fastcgi_pass unix:/run/php/php8.3-fpm-mcd-other.sock;
    }}
}}
""",
                encoding="utf-8",
            )
            (sites_enabled / vhost.name).symlink_to(vhost)
            (sites_enabled / other_vhost.name).symlink_to(other_vhost)

            with (
                patch.object(instance_runtime, "PHP_ETC_ROOT", php_etc),
                patch.object(instance_runtime, "GENERATED_ROOT", generated),
                patch.object(instance_runtime, "BACKUP_ROOT", backups),
                patch.object(instance_runtime, "NGINX_SITES_AVAILABLE", sites_available),
                patch.object(instance_runtime, "NGINX_SITES_ENABLED", sites_enabled),
                patch.object(instance_runtime, "_run", self._fake_run),
            ):
                payload = instance_runtime.apply_instance_runtime([inst], reload_services=False)

            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["changed"])
            self.assertIn("fastcgi_pass unix:/run/php/php8.3-fpm.sock;", vhost.read_text(encoding="utf-8"))
            self.assertIn("fastcgi_pass unix:/run/php/php8.3-fpm-mcd-other.sock;", other_vhost.read_text(encoding="utf-8"))
            self.assertTrue(include_file.exists())
            self.assertTrue(pool_link.is_symlink())
            self.assertNotIn("fpm_include_removed:8.3", payload["actions"])
            self.assertNotIn("fpm_link_removed:8.3", payload["actions"])

    def test_rolls_back_nginx_when_config_test_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            php_etc = base / "etc" / "php"
            generated = base / "opt" / "mcd" / "generated"
            backups = base / "backups"
            sites_available = base / "etc" / "nginx" / "sites-available"
            sites_enabled = base / "etc" / "nginx" / "sites-enabled"
            sites_available.mkdir(parents=True)
            sites_enabled.mkdir(parents=True)
            inst_root = base / "var" / "www" / "merkurosiguranje" / "public_html"
            inst = self._install(inst_root)
            original = f"""
server {{
    server_name merkurosiguranje.sales-snap.com;
    root {inst_root / "docroot"};
    location ~ \\.php$ {{
        fastcgi_pass unix:/run/php/php8.3-fpm-mcd-merkurosiguranje.sock;
    }}
}}
"""
            vhost = sites_available / "merkurosiguranje.sales-snap.com.conf"
            vhost.write_text(original, encoding="utf-8")
            (sites_enabled / vhost.name).symlink_to(vhost)

            def fail_nginx(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if cmd == ["nginx", "-t"]:
                    return subprocess.CompletedProcess(cmd, 1, "", "bad nginx")
                return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

            with (
                patch.object(instance_runtime, "PHP_ETC_ROOT", php_etc),
                patch.object(instance_runtime, "GENERATED_ROOT", generated),
                patch.object(instance_runtime, "BACKUP_ROOT", backups),
                patch.object(instance_runtime, "NGINX_SITES_AVAILABLE", sites_available),
                patch.object(instance_runtime, "NGINX_SITES_ENABLED", sites_enabled),
                patch.object(instance_runtime, "_run", fail_nginx),
            ):
                payload = instance_runtime.apply_instance_runtime([inst], reload_services=False)

            self.assertEqual(payload["status"], "error")
            self.assertEqual(vhost.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
