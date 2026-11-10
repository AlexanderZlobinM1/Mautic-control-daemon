from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.fail2ban_guard import (
    NGINX_4XX_SAFETY_MARKER,
    _is_path_agnostic_4xx_filter,
    ensure_nginx_4xx_scan_safety,
)


BROAD_FILTER = r'''[Definition]
failregex = ^<HOST>\s+.*"(?:GET|POST|HEAD|OPTIONS)\s+[^\"]+"\s+(?:400|403|404|444)\s+\d+
ignoreregex =
'''


class Fail2ban4xxSafetyTests(unittest.TestCase):
    def test_detects_path_agnostic_4xx_filter(self) -> None:
        self.assertTrue(_is_path_agnostic_4xx_filter(BROAD_FILTER))

    def test_keeps_path_scoped_filter_enabled(self) -> None:
        scoped = r'''[Definition]
failregex = ^<HOST> .*"GET /(?:wp-admin|\.env) HTTP/[^\"]+" (?:403|404) \d+
'''
        self.assertFalse(_is_path_agnostic_4xx_filter(scoped))

    def test_disables_broad_jail_and_preserves_other_jail_bans(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            filter_path = Path(td) / "nginx-4xx-scan.conf"
            override_path = Path(td) / "zzzz-mcd-nginx-4xx-safety.local"
            filter_path.write_text(BROAD_FILTER, encoding="utf-8")
            responses = [
                SimpleNamespace(returncode=0, stdout="Jail list: nginx-4xx-scan, nginx-php-probe\n", stderr=""),
                SimpleNamespace(returncode=0, stdout="Banned IP list: 192.0.2.10 192.0.2.11\n", stderr=""),
                SimpleNamespace(returncode=0, stdout="Banned IP list: 192.0.2.11\n", stderr=""),
                SimpleNamespace(returncode=0, stdout="", stderr=""),
                SimpleNamespace(returncode=0, stdout="Jail list: nginx-4xx-scan, nginx-php-probe\n", stderr=""),
                SimpleNamespace(returncode=0, stdout="Banned IP list: 192.0.2.10 192.0.2.11\n", stderr=""),
                SimpleNamespace(returncode=0, stdout="Banned IP list: 192.0.2.11\n", stderr=""),
            ]
            responses.extend(SimpleNamespace(returncode=0, stdout="", stderr="") for _ in range(7))
            with (
                patch("mcd_agent.fail2ban_guard.os.geteuid", return_value=0),
                patch("mcd_agent.fail2ban_guard.shutil.which", return_value="/usr/bin/fail2ban-client"),
                patch("mcd_agent.fail2ban_guard.subprocess.run", side_effect=responses) as run,
            ):
                result = ensure_nginx_4xx_scan_safety(
                    filter_path=filter_path,
                    override_path=override_path,
                )
                override = override_path.read_text(encoding="utf-8")

        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["released_bans"], 1)
        self.assertEqual(result["preserved_bans"], 1)
        self.assertIn(NGINX_4XX_SAFETY_MARKER, override)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["/usr/bin/fail2ban-client", "stop", "nginx-4xx-scan"], commands)
        self.assertIn(
            ["/usr/bin/fail2ban-client", "set", "nginx-php-probe", "banip", "192.0.2.11"],
            commands,
        )

    def test_inactive_jail_does_not_reload_when_override_is_current(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            filter_path = Path(td) / "nginx-4xx-scan.conf"
            override_path = Path(td) / "zzzz-mcd-nginx-4xx-safety.local"
            filter_path.write_text(BROAD_FILTER, encoding="utf-8")
            override_path.write_text(
                f"{NGINX_4XX_SAFETY_MARKER}\n[nginx-4xx-scan]\nenabled = false\n",
                encoding="utf-8",
            )
            status = SimpleNamespace(returncode=0, stdout="Jail list: nginx-php-probe\n", stderr="")
            with (
                patch("mcd_agent.fail2ban_guard.os.geteuid", return_value=0),
                patch("mcd_agent.fail2ban_guard.shutil.which", return_value="/usr/bin/fail2ban-client"),
                patch("mcd_agent.fail2ban_guard.subprocess.run", return_value=status) as run,
            ):
                result = ensure_nginx_4xx_scan_safety(
                    filter_path=filter_path,
                    override_path=override_path,
                )

        self.assertEqual(result["status"], "noop")
        self.assertFalse(result["changed"])
        self.assertEqual(run.call_count, 2)
        self.assertNotIn(
            ["/usr/bin/fail2ban-client", "reload"],
            [call.args[0] for call in run.call_args_list],
        )

    def test_safety_override_name_sorts_after_site_jail_files(self) -> None:
        self.assertGreater(
            "zzzz-mcd-nginx-4xx-safety.local",
            "sales-snap-mauticfarm02.local",
        )

    def test_noop_when_filter_is_path_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            filter_path = Path(td) / "nginx-4xx-scan.conf"
            filter_path.write_text("[Definition]\nfailregex = ^<HOST> GET /wp-admin 404\n", encoding="utf-8")
            result = ensure_nginx_4xx_scan_safety(filter_path=filter_path)

        self.assertEqual(result["status"], "noop")
        self.assertFalse(result["changed"])


if __name__ == "__main__":
    unittest.main()
