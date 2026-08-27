from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import form_embed
from mcd_agent.models import MauticInstall


def _install(root: Path) -> MauticInstall:
    return MauticInstall(
        instance_uid="form-example",
        name="example.sales-snap.com",
        root=str(root),
        console_path=str(root / "bin" / "console"),
        primary_domain="example.sales-snap.com",
        domains=["example.sales-snap.com"],
    )


class FormEmbedTests(unittest.TestCase):
    def test_normalize_origins_accepts_canonical_https_only(self) -> None:
        self.assertEqual(
            form_embed.normalize_origins(
                ["https://WWW.Example.com/", "https://www.example.com", "https://portal.example.com:8443"]
            ),
            ["https://www.example.com", "https://portal.example.com:8443"],
        )
        for invalid in ("*", "http://example.com", "https://example.com/path", "https://user@example.com"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(form_embed.FormEmbedError):
                    form_embed.normalize_origins([invalid])

    def test_render_uses_exact_dynamic_cors_origins(self) -> None:
        rendered = form_embed.render_form_embed_location(
            fastcgi_pass="unix:/run/php/php8.3-fpm.sock",
            frame_ancestors=["https://embed.example.com"],
            cors_origins=["https://app.example.com"],
        )
        self.assertIn("frame-ancestors 'self' https://embed.example.com", rendered)
        self.assertIn('if ($http_origin = "https://app.example.com")', rendered)
        self.assertIn("add_header Access-Control-Allow-Origin $mcd_form_cors_origin always;", rendered)
        self.assertIn('add_header Vary "Origin" always;', rendered)
        self.assertNotIn("Access-Control-Allow-Origin *", rendered)

    def test_sync_inserts_managed_block_and_keeps_existing_custom_form_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "var" / "www" / "example" / "public_html"
            root.mkdir(parents=True)
            available = base / "sites-available"
            enabled = base / "sites-enabled"
            available.mkdir()
            enabled.mkdir()
            site = available / "example.conf"
            site.write_text(
                """server {
    listen 443 ssl;
    server_name example.sales-snap.com;
    root %s;
    location / {
        try_files $uri /index.php$is_args$args;
    }
    location ~ \\.php$ {
        fastcgi_pass unix:/run/php/php8.3-fpm.sock;
    }
}
"""
                % root,
                encoding="utf-8",
            )
            (enabled / site.name).symlink_to(site)
            cfg = SimpleNamespace(
                form_embed_instance_settings={
                    "form-example": {
                        "enabled": True,
                        "frame_ancestors": ["https://embed.example.com"],
                        "cors_origins": ["https://app.example.com"],
                    }
                }
            )
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
            with (
                patch.object(form_embed, "NGINX_SITES_ENABLED", enabled),
                patch.object(form_embed, "BACKUP_ROOT", base / "backups"),
                patch.object(form_embed, "STATE_PATH", base / "state.json"),
                patch.object(form_embed.subprocess, "run", return_value=completed) as run,
            ):
                result = form_embed.sync_form_embed_settings(cfg, [_install(root)])

            rendered = site.read_text(encoding="utf-8")
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["changed"])
            self.assertIn("# BEGIN MCD managed form embed", rendered)
            self.assertIn("https://embed.example.com", rendered)
            self.assertEqual(run.call_args_list[0].args[0], ["nginx", "-t"])
            self.assertEqual(run.call_args_list[1].args[0], ["systemctl", "reload", "nginx"])

            original_custom = """server {
    listen 443 ssl;
    server_name example.sales-snap.com;
    root %s;
    location ^~ /form/ {
        fastcgi_pass unix:/run/php/custom-form.sock;
    }
    location / {
        try_files $uri /index.php$is_args$args;
    }
    location ~ \\.php$ {
        fastcgi_pass unix:/run/php/php8.3-fpm.sock;
    }
}
""" % root
            site.write_text(original_custom, encoding="utf-8")
            with (
                patch.object(form_embed, "NGINX_SITES_ENABLED", enabled),
                patch.object(form_embed, "BACKUP_ROOT", base / "backups-two"),
                patch.object(form_embed, "STATE_PATH", base / "state-two.json"),
                patch.object(form_embed.subprocess, "run", return_value=completed) as run,
            ):
                result = form_embed.sync_form_embed_settings(cfg, [_install(root)])
            self.assertEqual(result["instances"]["form-example"]["status"], "blocked")
            self.assertEqual(result["instances"]["form-example"]["reason"], "blocked_custom_form_location")
            self.assertEqual(site.read_text(encoding="utf-8"), original_custom)
            run.assert_not_called()

    def test_sync_rolls_back_every_vhost_when_nginx_test_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "var" / "www" / "example" / "public_html"
            root.mkdir(parents=True)
            available = base / "sites-available"
            enabled = base / "sites-enabled"
            available.mkdir()
            enabled.mkdir()
            site = available / "example.conf"
            original = """server {
    listen 80;
    server_name example.sales-snap.com;
    root %s;
    location / {
        try_files $uri /index.php$is_args$args;
    }
    location ~ \\.php$ {
        fastcgi_pass unix:/run/php/php8.3-fpm.sock;
    }
}
""" % root
            site.write_text(original, encoding="utf-8")
            (enabled / site.name).symlink_to(site)
            cfg = SimpleNamespace(form_embed_instance_settings={"form-example": {"enabled": True}})
            failing = SimpleNamespace(returncode=1, stdout="", stderr="invalid nginx")
            successful = SimpleNamespace(returncode=0, stdout="", stderr="")
            with (
                patch.object(form_embed, "NGINX_SITES_ENABLED", enabled),
                patch.object(form_embed, "BACKUP_ROOT", base / "backups"),
                patch.object(form_embed, "STATE_PATH", base / "state.json"),
                patch.object(form_embed.subprocess, "run", side_effect=[failing, successful, successful]),
            ):
                result = form_embed.sync_form_embed_settings(cfg, [_install(root)])

            self.assertEqual(result["status"], "error")
            self.assertEqual(site.read_text(encoding="utf-8"), original)
