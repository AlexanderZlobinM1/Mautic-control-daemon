from __future__ import annotations

import unittest

import mcd_agent.nginx_baseline as nginx_baseline


class NginxBaselineHardeningTests(unittest.TestCase):
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
