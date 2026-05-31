from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import mcd_agent.nginx_baseline as nginx_baseline


class NginxBaselineHardeningTests(unittest.TestCase):
    def test_hardening_snippet_blocks_project_internals(self) -> None:
        snippet = nginx_baseline._desired_hardening_snippet()

        self.assertIn("config|vendor|node_modules|tests|var|\\.git", snippet)
        self.assertIn("composer\\.(?:json|lock)", snippet)
        self.assertIn("package(?:-lock)?\\.json", snippet)
        self.assertIn("Strict-Transport-Security", snippet)
        self.assertIn("X-Frame-Options", snippet)

    def test_insert_hardening_include_per_server_after_server_name(self) -> None:
        src = """server {
    listen 80;
    server_name one.example.com;
    root /var/www/one;
}

server {
    listen 443 ssl;
    server_name two.example.com;
    root /var/www/two;
}
"""

        out, changed = nginx_baseline._insert_hardening_include(src)

        self.assertTrue(changed)
        self.assertEqual(out.count(nginx_baseline.HARDENING_INCLUDE), 2)
        self.assertIn("server_name one.example.com;\n    include /etc/nginx/snippets/mcd-mautic-hardening.conf;", out)
        self.assertIn("server_name two.example.com;\n    include /etc/nginx/snippets/mcd-mautic-hardening.conf;", out)

    def test_insert_hardening_include_is_idempotent(self) -> None:
        src = f"""server {{
    listen 80;
    server_name one.example.com;
    {nginx_baseline.HARDENING_INCLUDE}
    root /var/www/one;
}}
"""

        out, changed = nginx_baseline._insert_hardening_include(src)

        self.assertFalse(changed)
        self.assertEqual(out, src)

    def test_insert_hardening_include_fills_partial_multi_server_file(self) -> None:
        src = f"""server {{
    listen 80;
    server_name one.example.com;
    {nginx_baseline.HARDENING_INCLUDE}
}}
server {{
    listen 443 ssl;
    server_name two.example.com;
}}
"""

        out, changed = nginx_baseline._insert_hardening_include(src)

        self.assertTrue(changed)
        self.assertEqual(out.count(nginx_baseline.HARDENING_INCLUDE), 2)
        self.assertIn("server_name two.example.com;\n    include /etc/nginx/snippets/mcd-mautic-hardening.conf;", out)

    def test_security_headers_snippet_adds_missing_location_headers(self) -> None:
        src = """# existing snippet
add_header "X-Content-Type-Options" "nosniff";
#add_header "Strict-Transport-Security" "max-age=31536000";
add_header "Referrer-Policy" "strict-origin-when-cross-origin";
"""

        out = nginx_baseline._desired_security_headers_snippet(src)

        self.assertIn(nginx_baseline.SECURITY_HEADERS_START, out)
        self.assertIn('add_header X-Frame-Options "SAMEORIGIN" always;', out)
        self.assertIn('add_header Strict-Transport-Security "max-age=31536000" always;', out)
        self.assertIn('add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;', out)

    def test_security_headers_snippet_is_idempotent(self) -> None:
        src = """add_header X-Frame-Options "SAMEORIGIN" always;
add_header Strict-Transport-Security "max-age=31536000" always;
add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;
"""

        out = nginx_baseline._desired_security_headers_snippet(src)

        self.assertEqual(out, src)

    def test_active_server_config_files_resolves_enabled_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snippets = root / "snippets"
            conf_d = root / "conf.d"
            enabled = root / "sites-enabled"
            available = root / "sites-available"
            for d in (snippets, conf_d, enabled, available):
                d.mkdir()
            site = available / "site.example.com.conf"
            site.write_text("server { server_name site.example.com; }\n", encoding="utf-8")
            (enabled / site.name).symlink_to(site)
            confd = conf_d / "default.conf"
            confd.write_text("server { listen 80 default_server; }\n", encoding="utf-8")

            old_enabled = nginx_baseline.SITES_ENABLED
            old_available = nginx_baseline.SITES_AVAILABLE
            old_conf_d = nginx_baseline.CONF_D
            old_snippet_dir = nginx_baseline.SNIPPETS_DIR
            old_snippet = nginx_baseline.HARDENING_SNIPPET
            try:
                nginx_baseline.SITES_ENABLED = enabled
                nginx_baseline.SITES_AVAILABLE = available
                nginx_baseline.CONF_D = conf_d
                nginx_baseline.SNIPPETS_DIR = snippets
                nginx_baseline.HARDENING_SNIPPET = snippets / "mcd-mautic-hardening.conf"

                files = nginx_baseline._active_server_config_files()
            finally:
                nginx_baseline.SITES_ENABLED = old_enabled
                nginx_baseline.SITES_AVAILABLE = old_available
                nginx_baseline.CONF_D = old_conf_d
                nginx_baseline.SNIPPETS_DIR = old_snippet_dir
                nginx_baseline.HARDENING_SNIPPET = old_snippet

        self.assertEqual(set(files), {site.resolve(), confd.resolve()})

    def test_normalize_sites_enabled_symlink_requires_conf_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            enabled = root / "sites-enabled"
            available = root / "sites-available"
            backup = root / "backup"
            enabled.mkdir()
            available.mkdir()
            site = available / "s.apetit.rs"
            site.write_text("server { server_name s.apetit.rs; }\n", encoding="utf-8")
            old_link = enabled / "s.apetit.rs"
            old_link.symlink_to(site)

            old_enabled = nginx_baseline.SITES_ENABLED
            try:
                nginx_baseline.SITES_ENABLED = enabled
                actions = nginx_baseline._normalize_sites_enabled_conf_suffix(backup, {})
            finally:
                nginx_baseline.SITES_ENABLED = old_enabled

            self.assertIn("sites_enabled_conf_suffix:s.apetit.rs->s.apetit.rs.conf", actions)
            self.assertFalse(old_link.exists())
            self.assertTrue((enabled / "s.apetit.rs.conf").is_symlink())
            self.assertEqual((enabled / "s.apetit.rs.conf").resolve(), site.resolve())

    def test_convert_sites_enabled_regular_file_writes_conf_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            enabled = root / "sites-enabled"
            available = root / "sites-available"
            backup = root / "backup"
            enabled.mkdir()
            available.mkdir()
            legacy = enabled / "legacy.example.com"
            legacy.write_text("server { server_name legacy.example.com; }\n", encoding="utf-8")

            old_enabled = nginx_baseline.SITES_ENABLED
            old_available = nginx_baseline.SITES_AVAILABLE
            try:
                nginx_baseline.SITES_ENABLED = enabled
                nginx_baseline.SITES_AVAILABLE = available
                actions = nginx_baseline._convert_sites_enabled_regular_files(backup, {})
            finally:
                nginx_baseline.SITES_ENABLED = old_enabled
                nginx_baseline.SITES_AVAILABLE = old_available

            self.assertIn("sites_available_updated:legacy.example.com.conf", actions)
            self.assertIn("sites_enabled_symlink:legacy.example.com->legacy.example.com.conf", actions)
            self.assertFalse(legacy.exists())
            self.assertTrue((available / "legacy.example.com.conf").is_file())
            self.assertTrue((enabled / "legacy.example.com.conf").is_symlink())
            self.assertEqual((enabled / "legacy.example.com.conf").resolve(), (available / "legacy.example.com.conf").resolve())


if __name__ == "__main__":
    unittest.main()
