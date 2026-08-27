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
        self.assertIn("add_header X-Frame-Options $mcd_x_frame_options always;", snippet)

    def test_form_submit_response_can_load_in_cross_domain_messenger_iframe(self) -> None:
        config = nginx_baseline._desired_mautic_form_headers_config()

        self.assertIn("map $request_uri $mcd_x_frame_options", config)
        self.assertIn('default "SAMEORIGIN";', config)
        self.assertIn('~^/form/submit(?:\\?|$) "";', config)

    def test_fastcgi_php_snippet_supports_official_nginx_packages(self) -> None:
        snippet = nginx_baseline._desired_fastcgi_php_snippet()

        self.assertIn("fastcgi_split_path_info", snippet)
        self.assertIn("fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;", snippet)
        self.assertIn("include fastcgi_params;", snippet)

    def test_nginx_conf_baseline_includes_conf_d_and_sites_enabled(self) -> None:
        src = "user nginx;\nhttp {\n    worker_connections 1024;\n}\n"

        out, actions = nginx_baseline._desired_nginx_conf(src)

        self.assertIn("user_www_data", actions)
        self.assertIn("conf_d_include", actions)
        self.assertIn("sites_enabled_include", actions)
        self.assertIn("include /etc/nginx/conf.d/*.conf;", out)
        self.assertIn("include /etc/nginx/sites-enabled/*.conf;", out)

    def test_cloudflare_real_ip_template_uses_cf_connecting_ip(self) -> None:
        content = nginx_baseline._desired_cloudflare_real_ip_config(
            {
                "cloudflare_real_ip_enabled": True,
                "cloudflare_real_ip_cidrs": ["173.245.48.0/20", "173.245.48.0/20", "bad"],
            }
        )

        self.assertIn("real_ip_header CF-Connecting-IP;", content)
        self.assertIn("real_ip_recursive on;", content)
        self.assertEqual(content.count("set_real_ip_from 173.245.48.0/20;"), 1)

    def test_default_deny_vhost_rejects_unknown_hosts(self) -> None:
        content = nginx_baseline._desired_default_deny_config(
            (Path("/cert/fullchain.pem"), Path("/cert/privkey.pem")),
            default_server_ports=set(),
        )

        self.assertIn(nginx_baseline.DEFAULT_DENY_MARKER, content)
        self.assertIn("listen 80 default_server;", content)
        self.assertIn("listen 443 ssl default_server;", content)
        self.assertIn("server_name _;", content)
        self.assertEqual(content.count("return 444;"), 2)

    def test_default_deny_vhost_avoids_occupied_default_server_ports(self) -> None:
        content = nginx_baseline._desired_default_deny_config(
            (Path("/cert/fullchain.pem"), Path("/cert/privkey.pem")),
            default_server_ports={80},
        )

        self.assertNotIn("listen 80;", content)
        self.assertNotIn("listen 80 default_server;", content)
        self.assertIn("listen 443 ssl default_server;", content)

    def test_listen_default_server_ports_detects_legacy_default_vhost(self) -> None:
        src = """server {
    listen 80 default_server;
    listen [::]:80 default_server;
    listen 443 ssl default_server;
    # listen 8443 ssl default_server;
}
"""

        self.assertEqual(nginx_baseline._listen_default_server_ports(src), {80, 443})

    def test_ensure_default_deny_config_writes_managed_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            conf_d = root / "conf.d"
            conf_d.mkdir()
            target = conf_d / "00-mcd-default-deny.conf"
            backup = root / "backup"

            old_file = nginx_baseline.DEFAULT_DENY_FILE
            old_pair = nginx_baseline._default_deny_ssl_pair
            old_ports = nginx_baseline._active_default_server_ports
            try:
                nginx_baseline.DEFAULT_DENY_FILE = target
                nginx_baseline._default_deny_ssl_pair = lambda: (Path("/cert/fullchain.pem"), Path("/cert/privkey.pem"))
                nginx_baseline._active_default_server_ports = lambda: set()
                actions = nginx_baseline._ensure_default_deny_config(backup, {})
                text = target.read_text(encoding="utf-8")
            finally:
                nginx_baseline.DEFAULT_DENY_FILE = old_file
                nginx_baseline._default_deny_ssl_pair = old_pair
                nginx_baseline._active_default_server_ports = old_ports

        self.assertEqual(actions, ["default_deny_vhost"])
        self.assertIn("listen 80 default_server;", text)
        self.assertIn("listen 443 ssl default_server;", text)
        self.assertIn("return 444;", text)

    def test_ensure_cloudflare_real_ip_writes_managed_conf(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            conf_d = root / "conf.d"
            sites_available = root / "sites-available"
            sites_enabled = root / "sites-enabled"
            snippets = root / "snippets"
            conf_d.mkdir()
            sites_available.mkdir()
            sites_enabled.mkdir()
            snippets.mkdir()
            nginx_conf = root / "nginx.conf"
            nginx_conf.write_text("user www-data;\nhttp {\n    include /etc/nginx/conf.d/*.conf;\n    include /etc/nginx/sites-enabled/*.conf;\n}\n", encoding="utf-8")
            target = conf_d / "10-mcd-cloudflare-real-ip.conf"

            old_conf = nginx_baseline.NGINX_CONF
            old_conf_d = nginx_baseline.CONF_D
            old_available = nginx_baseline.SITES_AVAILABLE
            old_enabled = nginx_baseline.SITES_ENABLED
            old_snippets = nginx_baseline.SNIPPETS_DIR
            old_hardening = nginx_baseline.HARDENING_SNIPPET
            old_fastcgi = nginx_baseline.FASTCGI_PHP_SNIPPET
            old_default_deny = nginx_baseline.DEFAULT_DENY_FILE
            old_default_pair = nginx_baseline._default_deny_ssl_pair
            old_present = nginx_baseline._nginx_present
            old_test = nginx_baseline._nginx_test
            old_reload = nginx_baseline._reload_nginx
            old_geteuid = nginx_baseline.os.geteuid
            try:
                nginx_baseline.NGINX_CONF = nginx_conf
                nginx_baseline.CONF_D = conf_d
                nginx_baseline.SITES_AVAILABLE = sites_available
                nginx_baseline.SITES_ENABLED = sites_enabled
                nginx_baseline.SNIPPETS_DIR = snippets
                nginx_baseline.HARDENING_SNIPPET = snippets / "mcd-mautic-hardening.conf"
                nginx_baseline.FASTCGI_PHP_SNIPPET = snippets / "fastcgi-php.conf"
                nginx_baseline.DEFAULT_DENY_FILE = conf_d / "00-mcd-default-deny.conf"
                nginx_baseline._default_deny_ssl_pair = lambda: (Path("/cert/fullchain.pem"), Path("/cert/privkey.pem"))
                nginx_baseline._nginx_present = lambda: True
                nginx_baseline._nginx_test = lambda: (True, "nginx_test:ok")
                nginx_baseline._reload_nginx = lambda: (True, "nginx_reload:ok")
                nginx_baseline.os.geteuid = lambda: 0

                result = nginx_baseline.ensure_cloudflare_real_ip(
                    {
                        "cloudflare_real_ip_enabled": True,
                        "cloudflare_real_ip_target_file": str(target),
                        "cloudflare_real_ip_cidrs": ["173.245.48.0/20"],
                    }
                )
            finally:
                nginx_baseline.NGINX_CONF = old_conf
                nginx_baseline.CONF_D = old_conf_d
                nginx_baseline.SITES_AVAILABLE = old_available
                nginx_baseline.SITES_ENABLED = old_enabled
                nginx_baseline.SNIPPETS_DIR = old_snippets
                nginx_baseline.HARDENING_SNIPPET = old_hardening
                nginx_baseline.FASTCGI_PHP_SNIPPET = old_fastcgi
                nginx_baseline.DEFAULT_DENY_FILE = old_default_deny
                nginx_baseline._default_deny_ssl_pair = old_default_pair
                nginx_baseline._nginx_present = old_present
                nginx_baseline._nginx_test = old_test
                nginx_baseline._reload_nginx = old_reload
                nginx_baseline.os.geteuid = old_geteuid

            self.assertEqual(result["status"], "applied")
            text = target.read_text(encoding="utf-8")
            self.assertIn(nginx_baseline.CLOUDFLARE_REAL_IP_MARKER, text)
            self.assertIn("set_real_ip_from 173.245.48.0/20;", text)
            self.assertIn("real_ip_header CF-Connecting-IP;", text)

    def test_ensure_cloudflare_real_ip_removes_managed_conf_for_direct_edge(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            conf_d = root / "conf.d"
            conf_d.mkdir()
            target = conf_d / "10-mcd-cloudflare-real-ip.conf"
            target.write_text(nginx_baseline.CLOUDFLARE_REAL_IP_MARKER + "\nreal_ip_header CF-Connecting-IP;\n", encoding="utf-8")

            old_conf_d = nginx_baseline.CONF_D
            old_test = nginx_baseline._nginx_test
            old_reload = nginx_baseline._reload_nginx
            old_geteuid = nginx_baseline.os.geteuid
            try:
                nginx_baseline.CONF_D = conf_d
                nginx_baseline._nginx_test = lambda: (True, "nginx_test:ok")
                nginx_baseline._reload_nginx = lambda: (True, "nginx_reload:ok")
                nginx_baseline.os.geteuid = lambda: 0

                result = nginx_baseline.ensure_cloudflare_real_ip(
                    {
                        "cloudflare_real_ip_enabled": False,
                        "cloudflare_real_ip_target_file": str(target),
                        "cloudflare_real_ip_remove_when_disabled": True,
                    }
                )
            finally:
                nginx_baseline.CONF_D = old_conf_d
                nginx_baseline._nginx_test = old_test
                nginx_baseline._reload_nginx = old_reload
                nginx_baseline.os.geteuid = old_geteuid

            self.assertEqual(result["status"], "applied")
            self.assertFalse(target.exists())

    def test_public_app_asset_locations_precede_private_app_deny(self) -> None:
        src = """server {
    server_name example.com;
    root /var/www/example/public_html/docroot;

    location ~* ^/(?:app|bin|config|vendor|var)/ {
        deny all;
    }
}
"""

        out = nginx_baseline.ensure_mautic_public_app_asset_locations(src)

        self.assertIn("^/app/bundles/.*/Assets/", out)
        self.assertIn("^/app/assets/", out)
        self.assertLess(out.index("^/app/assets/"), out.index("^/(?:app|bin|config|vendor|var)"))
        self.assertEqual(nginx_baseline.ensure_mautic_public_app_asset_locations(out), out)

    def test_legacy_http2_listen_is_modernized_when_supported(self) -> None:
        src = """server {
    listen 80;
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name example.com;
}
"""

        out = nginx_baseline.normalize_legacy_http2_listen(src, modern_http2=True)

        self.assertIn("listen 443 ssl;", out)
        self.assertIn("listen [::]:443 ssl;", out)
        self.assertEqual(out.count("http2 on;"), 1)
        self.assertNotIn("ssl http2;", out)

    def test_ipv6_listen_directives_are_removed_for_ipv4_only_hosts(self) -> None:
        src = """server {
    listen 80;
    listen [::]:80;
    listen 443 ssl;
    listen [::]:443 ssl;
    # listen [::]:8080;
    server_name example.com;
}
"""

        out = nginx_baseline.remove_ipv6_listen_directives(src)

        self.assertIn("listen 80;", out)
        self.assertIn("listen 443 ssl;", out)
        self.assertNotIn("listen [::]:80;", out)
        self.assertNotIn("listen [::]:443 ssl;", out)
        self.assertIn("# listen [::]:8080;", out)

    def test_duplicate_server_autoindex_is_removed_when_hardening_is_included(self) -> None:
        src = f"""server {{
    listen 80;
    server_name example.com;
    {nginx_baseline.HARDENING_INCLUDE}
    autoindex off;

    location /files/ {{
        autoindex on;
    }}
}}
"""

        out = nginx_baseline.remove_duplicate_server_autoindex_directives(src)

        self.assertIn(nginx_baseline.HARDENING_INCLUDE, out)
        self.assertNotIn("\n    autoindex off;\n", out)
        self.assertIn("        autoindex on;", out)

    def test_server_config_normalization_updates_active_vhost(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            available = root / "sites-available"
            enabled = root / "sites-enabled"
            backup = root / "backup"
            available.mkdir()
            enabled.mkdir()
            site = available / "example.com.conf"
            site.write_text(
                """server {
    listen 443 ssl http2;
    server_name example.com;

    location ~* ^/(?:app|bin|config|vendor|var)/ {
        deny all;
    }
}
""",
                encoding="utf-8",
            )
            (enabled / site.name).symlink_to(site)

            old_available = nginx_baseline.SITES_AVAILABLE
            old_enabled = nginx_baseline.SITES_ENABLED
            old_conf_d = nginx_baseline.CONF_D
            old_http2 = nginx_baseline._nginx_supports_http2_directive
            old_ipv6 = nginx_baseline._ipv6_listen_forbidden
            try:
                nginx_baseline.SITES_AVAILABLE = available
                nginx_baseline.SITES_ENABLED = enabled
                nginx_baseline.CONF_D = root / "conf.d"
                nginx_baseline._nginx_supports_http2_directive = lambda: True
                nginx_baseline._ipv6_listen_forbidden = lambda: False
                actions = nginx_baseline._ensure_server_config_normalization(backup, {})
            finally:
                nginx_baseline.SITES_AVAILABLE = old_available
                nginx_baseline.SITES_ENABLED = old_enabled
                nginx_baseline.CONF_D = old_conf_d
                nginx_baseline._nginx_supports_http2_directive = old_http2
                nginx_baseline._ipv6_listen_forbidden = old_ipv6

            text = site.read_text(encoding="utf-8")
            self.assertIn("mautic_public_app_assets:example.com.conf", actions)
            self.assertIn("http2_listen_modernized:example.com.conf", actions)
            self.assertIn("^/app/assets/", text)
            self.assertIn("http2 on;", text)

    def test_server_config_normalization_removes_ipv6_listen_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            available = root / "sites-available"
            enabled = root / "sites-enabled"
            backup = root / "backup"
            available.mkdir()
            enabled.mkdir()
            site = available / "example.com.conf"
            site.write_text(
                """server {
    listen 80;
    listen [::]:80;
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name example.com;
}
""",
                encoding="utf-8",
            )
            (enabled / site.name).symlink_to(site)

            old_available = nginx_baseline.SITES_AVAILABLE
            old_enabled = nginx_baseline.SITES_ENABLED
            old_conf_d = nginx_baseline.CONF_D
            old_http2 = nginx_baseline._nginx_supports_http2_directive
            old_ipv6 = nginx_baseline._ipv6_listen_forbidden
            try:
                nginx_baseline.SITES_AVAILABLE = available
                nginx_baseline.SITES_ENABLED = enabled
                nginx_baseline.CONF_D = root / "conf.d"
                nginx_baseline._nginx_supports_http2_directive = lambda: True
                nginx_baseline._ipv6_listen_forbidden = lambda: True
                actions = nginx_baseline._ensure_server_config_normalization(backup, {})
            finally:
                nginx_baseline.SITES_AVAILABLE = old_available
                nginx_baseline.SITES_ENABLED = old_enabled
                nginx_baseline.CONF_D = old_conf_d
                nginx_baseline._nginx_supports_http2_directive = old_http2
                nginx_baseline._ipv6_listen_forbidden = old_ipv6

            text = site.read_text(encoding="utf-8")
            self.assertIn("ipv6_listen_removed:example.com.conf", actions)
            self.assertIn("http2_listen_modernized:example.com.conf", actions)
            self.assertIn("listen 80;", text)
            self.assertIn("listen 443 ssl;", text)
            self.assertNotIn("[::]", text)
            self.assertIn("http2 on;", text)

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
        self.assertIn("add_header X-Frame-Options $mcd_x_frame_options always;", out)
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

    def test_baseline_requires_sites_directories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            nginx_conf = root / "nginx.conf"
            nginx_conf.write_text(
                "user www-data;\nhttp {\n    include /etc/nginx/sites-enabled/*.conf;\n}\n",
                encoding="utf-8",
            )
            snippets = root / "snippets"
            snippets.mkdir()
            hardening = snippets / "mcd-mautic-hardening.conf"
            hardening.write_text(nginx_baseline._desired_hardening_snippet(), encoding="utf-8")
            fastcgi = snippets / "fastcgi-php.conf"
            fastcgi.write_text(nginx_baseline._desired_fastcgi_php_snippet(), encoding="utf-8")

            old_conf = nginx_baseline.NGINX_CONF
            old_available = nginx_baseline.SITES_AVAILABLE
            old_enabled = nginx_baseline.SITES_ENABLED
            old_snippet = nginx_baseline.HARDENING_SNIPPET
            old_fastcgi = nginx_baseline.FASTCGI_PHP_SNIPPET
            try:
                nginx_baseline.NGINX_CONF = nginx_conf
                nginx_baseline.SITES_AVAILABLE = root / "sites-available"
                nginx_baseline.SITES_ENABLED = root / "sites-enabled"
                nginx_baseline.HARDENING_SNIPPET = hardening
                nginx_baseline.FASTCGI_PHP_SNIPPET = fastcgi

                self.assertFalse(nginx_baseline.nginx_baseline_satisfied())
            finally:
                nginx_baseline.NGINX_CONF = old_conf
                nginx_baseline.SITES_AVAILABLE = old_available
                nginx_baseline.SITES_ENABLED = old_enabled
                nginx_baseline.HARDENING_SNIPPET = old_snippet
                nginx_baseline.FASTCGI_PHP_SNIPPET = old_fastcgi

    def test_ensure_sites_directories_creates_debian_layout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            available = root / "sites-available"
            enabled = root / "sites-enabled"

            old_available = nginx_baseline.SITES_AVAILABLE
            old_enabled = nginx_baseline.SITES_ENABLED
            try:
                nginx_baseline.SITES_AVAILABLE = available
                nginx_baseline.SITES_ENABLED = enabled
                actions = nginx_baseline._ensure_sites_directories()
                self.assertEqual(actions, [f"created:{available}", f"created:{enabled}"])
                self.assertTrue(available.is_dir())
                self.assertTrue(enabled.is_dir())
            finally:
                nginx_baseline.SITES_AVAILABLE = old_available
                nginx_baseline.SITES_ENABLED = old_enabled

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

    def test_write_fastcgi_php_snippet_creates_missing_compat_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snippets = root / "snippets"
            fastcgi = snippets / "fastcgi-php.conf"
            backup = root / "backup"

            old_snippets = nginx_baseline.SNIPPETS_DIR
            old_fastcgi = nginx_baseline.FASTCGI_PHP_SNIPPET
            try:
                nginx_baseline.SNIPPETS_DIR = snippets
                nginx_baseline.FASTCGI_PHP_SNIPPET = fastcgi
                actions = nginx_baseline._write_fastcgi_php_snippet(backup, {})
            finally:
                nginx_baseline.SNIPPETS_DIR = old_snippets
                nginx_baseline.FASTCGI_PHP_SNIPPET = old_fastcgi

            self.assertEqual(actions, ["fastcgi_php_snippet"])
            self.assertEqual(fastcgi.read_text(encoding="utf-8"), nginx_baseline._desired_fastcgi_php_snippet())


if __name__ == "__main__":
    unittest.main()
