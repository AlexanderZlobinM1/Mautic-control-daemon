from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcd_agent import local_mail


class LocalMailTests(unittest.TestCase):
    def test_runtime_keeps_python_310_compatible_utc_import(self) -> None:
        source = Path(local_mail.__file__).read_text(encoding="utf-8")

        self.assertNotIn("from datetime import UTC", source)
        self.assertIn("datetime.now(timezone.utc)", source)

    def test_mail_test_supports_returned_and_assignment_style_local_php(self) -> None:
        script = local_mail._MAIL_TEST_SCRIPT
        self.assertIn("$included = include $argv[1]", script)
        self.assertIn("is_array($included)", script)
        self.assertIn("isset($parameters) && is_array($parameters)", script)

    def test_clean_host_installs_postfix_and_opendkim(self) -> None:
        installed = {
            "sendmail-bin": False,
            "postfix": False,
            "opendkim": False,
            "opendkim-tools": False,
            "sudo": False,
        }
        commands = []

        def fake_run(args, **_kwargs):
            commands.append(args)
            if args[:2] == ["dpkg-query", "-W"]:
                return (0, "installed") if installed.get(args[-1]) else (1, "")
            if args == ["debconf-set-selections"] or args[:2] == ["apt-get", "update"]:
                return 0, ""
            if args[:4] == ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install"]:
                self.assertEqual(args[4:], ["-y", "postfix", "opendkim", "opendkim-tools", "sudo"])
                installed.update({"postfix": True, "opendkim": True, "opendkim-tools": True, "sudo": True})
                return 0, ""
            self.fail(f"unexpected command: {args}")

        def fake_which(name):
            if name == "sendmail" and installed["postfix"]:
                return "/usr/sbin/sendmail"
            if name == "opendkim" and installed["opendkim"]:
                return "/usr/sbin/opendkim"
            if name == "opendkim-testkey" and installed["opendkim-tools"]:
                return "/usr/sbin/opendkim-testkey"
            if name == "sudo" and installed["sudo"]:
                return "/usr/bin/sudo"
            return None

        with patch.object(local_mail, "_run", side_effect=fake_run), patch.object(
            local_mail.shutil, "which", side_effect=fake_which
        ):
            self.assertEqual(local_mail._apt_install(), "postfix")
        self.assertIn(["debconf-set-selections"], commands)

    def test_apt_install_adds_only_missing_native_packages(self) -> None:
        installed = {"sendmail-bin": True, "opendkim": False, "opendkim-tools": False}

        def fake_run(args, **_kwargs):
            if args[:2] == ["dpkg-query", "-W"]:
                package = args[-1]
                return (0, "installed") if installed.get(package) else (1, "")
            if args[:2] == ["apt-get", "update"]:
                return 0, ""
            if args[:4] == ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install"]:
                self.assertEqual(args[4:], ["-y", "opendkim", "opendkim-tools"])
                installed["opendkim"] = True
                installed["opendkim-tools"] = True
                return 0, ""
            self.fail(f"unexpected command: {args}")

        def fake_which(name):
            if name == "sudo":
                return "/usr/bin/sudo"
            if name == "sendmail" or installed.get("opendkim" if name == "opendkim" else "opendkim-tools"):
                return "/usr/sbin/" + name
            return None

        with patch.object(local_mail, "_run", side_effect=fake_run), patch.object(
            local_mail.shutil, "which", side_effect=fake_which
        ):
            local_mail._apt_install()

    def test_apt_install_refuses_to_replace_foreign_mta(self) -> None:
        def fake_run(args, **_kwargs):
            if args[:2] == ["dpkg-query", "-W"]:
                return 1, ""
            self.fail(f"package changes must not run: {args}")

        with patch.object(local_mail, "_run", side_effect=fake_run), patch.object(
            local_mail.shutil, "which", side_effect=lambda name: "/usr/sbin/sendmail" if name == "sendmail" else None
        ):
            with self.assertRaisesRegex(RuntimeError, "will not replace"):
                local_mail._apt_install()

    def test_sendmail_transform_disables_legacy_relay_and_masquerade(self) -> None:
        source = """divert(-1)dnl
define(`SMART_HOST', `mail.sales-snap.com')dnl
define(`confDOMAIN_NAME', `old.example.com')dnl
FEATURE(`allmasquerade')dnl
FEATURE(`masquerade_envelope')dnl
MASQUERADE_AS(`sales-snap.com')dnl
MAILER(`smtp')dnl
"""
        result = local_mail._sendmail_direct_text(source, "mail.app.sales-snap.com")
        active = [line for line in result.splitlines() if not line.lstrip().startswith("dnl")]
        self.assertFalse(any("SMART_HOST" in line for line in active))
        self.assertFalse(any("MASQUERADE" in line for line in active))
        self.assertIn("define(`confDOMAIN_NAME', `mail.app.sales-snap.com')dnl", result)
        self.assertIn("Port=2525, Addr=127.0.0.1", result)
        self.assertIn("define(`QUEUE_DIR', `/var/spool/mqueue-mcd')", result)
        self.assertEqual(result.count(local_mail._SENDMAIL_BEGIN), 1)
        self.assertLess(result.index(local_mail._SENDMAIL_BEGIN), result.index("MAILER(`smtp')dnl"))

    def test_sendmail_inbound_transform_preserves_relay_and_opens_only_mta_listener(self) -> None:
        source = """FEATURE(`no_default_msa')dnl
DAEMON_OPTIONS(`Family=inet, Name=MTA-v4, Port=smtp, Addr=127.0.0.1')dnl
DAEMON_OPTIONS(`Family=inet6, Name=MTA-v6, Port=25, Addr=::1')dnl
DAEMON_OPTIONS(`Family=inet, Name=MSP-v4, Port=submission, Addr=127.0.0.1')dnl
define(`SMART_HOST', `mail.sales-snap.com')dnl
MAILER(`local')dnl
MAILER(`smtp')dnl
"""

        result = local_mail._sendmail_inbound_text(source)
        repeated = local_mail._sendmail_inbound_text(result)

        self.assertIn("define(`SMART_HOST', `mail.sales-snap.com')dnl", result)
        self.assertIn("Name=MCD-Inbound, Port=smtp", result)
        self.assertIn("Port=submission, Addr=127.0.0.1", result)
        self.assertIn("FEATURE(`virtusertable'", result)
        self.assertEqual(repeated.count(local_mail._SENDMAIL_INBOUND_BEGIN), 1)
        active = [line for line in result.splitlines() if not line.lstrip().startswith("dnl")]
        self.assertFalse(any("Port=smtp" in line and "Addr=127.0.0.1" in line for line in active))
        self.assertFalse(any("Port=25" in line and "Addr=::1" in line for line in active))

    def test_receive_root_helper_restricts_mta_user_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            helper = Path(td) / "mcd-mail-receive-root"
            wrapper = Path(td) / "mcd-mail-receive"
            sudoers = Path(td) / "sudoers"
            with patch.object(local_mail, "RECEIVE_ROOT_HELPER", helper), patch.object(
                local_mail, "RECEIVE_WRAPPER", wrapper
            ), patch.object(local_mail, "RECEIVE_SUDOERS", sudoers), patch.object(
                local_mail.pwd, "getpwnam", return_value=object()
            ), patch.object(local_mail, "_run", return_value=(0, "")):
                local_mail._write_receive_wrapper()

            text = helper.read_text(encoding="utf-8")
            self.assertIn('[ "$#" -eq 2 ]', text)
            self.assertIn("--instance-domain=*", text)
            self.assertIn("--kind=bounce|--kind=feedback_loop", text)
            self.assertNotIn('receive "$@"', text)

    def test_smtp_firewall_is_exact_idempotent_and_systemd_managed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            helper = Path(td) / "mcd-local-mail-firewall"
            service = Path(td) / "mcd-local-mail-firewall.service"
            calls = []

            def fake_run(args, **_kwargs):
                calls.append(args)
                return 0, ""

            with patch.object(local_mail, "SMTP_FIREWALL_HELPER", helper), patch.object(
                local_mail, "SMTP_FIREWALL_SERVICE", service
            ), patch.object(local_mail.shutil, "which", return_value="/usr/sbin/iptables"), patch.object(
                local_mail, "_run", side_effect=fake_run
            ):
                local_mail._configure_smtp_firewall()

            helper_text = helper.read_text(encoding="utf-8")
            self.assertIn("--dport 25", helper_text)
            self.assertIn(local_mail._SMTP_FIREWALL_COMMENT, helper_text)
            self.assertIn("-C INPUT", helper_text)
            self.assertIn("-I INPUT 1", helper_text)
            self.assertIn("-D INPUT", helper_text)
            self.assertIn("RemainAfterExit=yes", service.read_text(encoding="utf-8"))
            self.assertIn(["systemctl", "enable", "--now", local_mail._SMTP_FIREWALL_SERVICE_NAME], calls)

    def test_inbound_routes_are_exact_and_reject_other_domain_mailboxes(self) -> None:
        aliases, virtual = local_mail._inbound_route_lines({"app.sales-snap.com": {}})

        self.assertEqual(len(aliases), 2)
        self.assertTrue(any("--kind=bounce" in line for line in aliases))
        self.assertTrue(any("--kind=feedback_loop" in line for line in aliases))
        self.assertTrue(any(line.startswith("bounce@app.sales-snap.com ") for line in virtual))
        self.assertTrue(any(line.startswith("fbl@app.sales-snap.com ") for line in virtual))
        self.assertTrue(any(line.startswith("abuse@app.sales-snap.com ") for line in virtual))
        self.assertIn("@app.sales-snap.com error:5.1.1:550 No such own-host mailbox", virtual)

    def test_sendmail_configuration_does_not_modify_system_config_or_queue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            system_mc = base / "system-sendmail.mc"
            isolated_mc = base / "mcd" / "sendmail.mc"
            isolated_cf = base / "mcd" / "sendmail.cf"
            isolated_queue = base / "mqueue-mcd"
            isolated_service = base / "mcd-local-mail-sendmail.service"
            source = "define(`SMART_HOST', `mail.sales-snap.com')dnl\nMAILER(`smtp')dnl\n"
            system_mc.write_text(source, encoding="utf-8")
            compiled = subprocess.CompletedProcess(
                ["m4"],
                0,
                stdout=f"compiled config\nO QueueDirectory={isolated_queue}\n".encode(),
                stderr=b"",
            )
            patches = (
                patch.object(local_mail, "SENDMAIL_MC", system_mc),
                patch.object(local_mail, "SENDMAIL_ISOLATED_MC", isolated_mc),
                patch.object(local_mail, "SENDMAIL_ISOLATED_CF", isolated_cf),
                patch.object(local_mail, "SENDMAIL_ISOLATED_QUEUE", isolated_queue),
                patch.object(local_mail, "SENDMAIL_ISOLATED_SERVICE", isolated_service),
                patch.object(local_mail.subprocess, "run", return_value=compiled),
                patch.object(local_mail, "_run", return_value=(0, "")),
                patch.object(local_mail.shutil, "chown"),
            )
            for item in patches:
                item.start()
            self.addCleanup(lambda: [item.stop() for item in reversed(patches)])

            local_mail._configure_sendmail("mail.app.sales-snap.com")

            self.assertEqual(system_mc.read_text(encoding="utf-8"), source)
            self.assertFalse((base / "mqueue").exists())
            self.assertIn(
                f"O QueueDirectory={isolated_queue}",
                isolated_cf.read_text(encoding="utf-8"),
            )
            self.assertIn("127.0.0.1", isolated_mc.read_text(encoding="utf-8"))
            service_text = isolated_service.read_text(encoding="utf-8")
            self.assertIn("sendmail.cf -bD", service_text)
            self.assertIn("KillSignal=SIGINT", service_text)
            self.assertIn("TimeoutStopSec=15s", service_text)

    def test_sendmail_configuration_rejects_compiled_system_queue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            system_mc = base / "system-sendmail.mc"
            system_mc.write_text("MAILER(`smtp')dnl\n", encoding="utf-8")
            isolated_service = base / "mcd-local-mail-sendmail.service"
            compiled = subprocess.CompletedProcess(
                ["m4"],
                0,
                stdout=b"O QueueDirectory=/var/spool/mqueue\n",
                stderr=b"",
            )
            with patch.object(local_mail, "SENDMAIL_MC", system_mc), patch.object(
                local_mail, "SENDMAIL_ISOLATED_MC", base / "mcd" / "sendmail.mc"
            ), patch.object(local_mail, "SENDMAIL_ISOLATED_CF", base / "mcd" / "sendmail.cf"), patch.object(
                local_mail, "SENDMAIL_ISOLATED_SERVICE", isolated_service
            ), patch.object(local_mail.subprocess, "run", return_value=compiled):
                with self.assertRaisesRegex(RuntimeError, "dedicated queue path"):
                    local_mail._configure_sendmail("mail.app.sales-snap.com")
            self.assertFalse(isolated_service.exists())

    def test_mautic_patch_uses_quota_wrapper_and_instance_from_domain(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "local.php"
            path.write_text(
                "<?php return ['parameters'=>['mailer_dsn'=>'smtp://localhost:25','mailer_from_email'=>'hello@example.com','mailer_return_path'=>null]];\n",
                encoding="utf-8",
            )
            with patch.object(local_mail, "_local_php", return_value=path):
                local_mail._configure_mautic("/var/www/app/public_html", "app.sales-snap.com")
            result = path.read_text(encoding="utf-8")
            self.assertIn(
                "mcd-mail-submit%%20--instance-domain%%3Dapp.sales-snap.com%%20--%%20-oi%%20-t",
                result,
            )
            self.assertIn("'mailer_from_email'=>'mailer@app.sales-snap.com'", result)
            self.assertIn("'mailer_return_path'=>'bounce@app.sales-snap.com'", result)
            self.assertNotIn("/.mcd/", result)

    def test_mautic_patch_adds_missing_mail_parameters_to_return_style_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "local.php"
            path.write_text(
                "<?php return ['parameters'=>['db_driver'=>'pdo_mysql','db_name'=>'app']];\n",
                encoding="utf-8",
            )
            with patch.object(local_mail, "_local_php", return_value=path):
                local_mail._configure_mautic("/var/www/app/public_html", "app.sales-snap.com")
            result = path.read_text(encoding="utf-8")

        self.assertIn("'parameters'=>[", result)
        self.assertIn("'mailer_dsn' =>", result)
        self.assertIn("'mailer_from_email' => 'mailer@app.sales-snap.com'", result)
        self.assertIn("'mailer_return_path' => 'bounce@app.sales-snap.com'", result)
        self.assertIn("'db_driver'=>'pdo_mysql'", result)

    def test_mautic_patch_adds_missing_mail_parameters_to_assignment_style_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "local.php"
            path.write_text(
                "<?php\n$parameters = array(\n    'db_driver' => 'pdo_mysql',\n);\n",
                encoding="utf-8",
            )
            with patch.object(local_mail, "_local_php", return_value=path):
                local_mail._configure_mautic("/var/www/app/public_html", "app.sales-snap.com")
            result = path.read_text(encoding="utf-8")

        self.assertIn("$parameters = array(", result)
        self.assertIn("'mailer_dsn' =>", result)
        self.assertIn("'mailer_from_email' => 'mailer@app.sales-snap.com'", result)
        self.assertIn("'mailer_return_path' => 'bounce@app.sales-snap.com'", result)

    def test_mail_test_unescapes_symfony_percent_literals(self) -> None:
        self.assertIn("str_replace('%%', '%', $dsn)", local_mail._MAIL_TEST_SCRIPT)

    def test_mail_test_omits_return_path_when_local_php_is_blank(self) -> None:
        self.assertIn("if ($returnPath !== '') { $email->returnPath($returnPath); }", local_mail._MAIL_TEST_SCRIPT)
        self.assertNotIn("->to($argv[3])\n    ->returnPath($returnPath)", local_mail._MAIL_TEST_SCRIPT)

    def test_inbound_bounce_is_applied_directly_to_the_matching_instance(self) -> None:
        raw = b"""From: MAILER-DAEMON@example.net
To: bounce@app.sales-snap.com
Subject: Delivery failure
Content-Type: multipart/report; report-type=delivery-status; boundary=x

--x
Content-Type: text/plain

Delivery failed.
--x
Content-Type: message/delivery-status

Final-Recipient: rfc822; customer@example.com
Action: failed
Status: 5.1.1
--x--
"""
        db = SimpleNamespace(add_dnc_for_emails=lambda *args, **kwargs: {"contacts": 1, "added": 1, "existing": 0})
        install = SimpleNamespace(
            primary_domain="app.sales-snap.com",
            domains=["app.sales-snap.com"],
            root="/var/www/app/public_html",
            db=object(),
        )
        cfg = SimpleNamespace(
            discovery_roots=["/var/www"],
            exclude_path_contains=[],
            supported_mautic_majors=[6],
            custom_instances=[],
        )
        with patch.object(
            local_mail,
            "_load_domains",
            return_value={"schema": 1, "domains": {"app.sales-snap.com": {"root": install.root}}},
        ), patch("mcd_agent.discovery.discover_mautic", return_value=[install]), patch(
            "mcd_agent.db.MauticDB", return_value=db
        ):
            result = local_mail.receive_local_mail(
                cfg,
                domain="app.sales-snap.com",
                kind="bounce",
                data=raw,
            )

        self.assertEqual(result["status"], "processed")
        self.assertEqual(result["dnc_added"], 1)
        self.assertEqual(result["contacts"], 1)

    def test_quota_counts_recipients_and_rolls_back_sendmail_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            config_root = base / "etc"
            state_root = base / "state"
            domains_path = config_root / "domains.json"
            config_root.mkdir()
            domains_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "domains": {
                            "app.sales-snap.com": {
                                "daily_limit": 3,
                                "monthly_limit": 3,
                                "mail_hostname": "mail.app.sales-snap.com",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            patches = (
                patch.object(local_mail, "CONFIG_ROOT", config_root),
                patch.object(local_mail, "STATE_ROOT", state_root),
                patch.object(local_mail, "DOMAINS_PATH", domains_path),
                patch.object(local_mail, "QUOTA_DB_PATH", state_root / "quota.sqlite3"),
                patch.object(local_mail, "_deliver_message", return_value=(0, 0)),
            )
            for item in patches:
                item.start()
            self.addCleanup(lambda: [item.stop() for item in reversed(patches)])
            data = b"From: mailer@app.sales-snap.com\nTo: one@example.com, two@example.com\n\nhello\n"
            self.assertEqual(local_mail.submit_local_mail(domain="app.sales-snap.com", sendmail_args=["--", "-t"], data=data), 0)
            self.assertEqual(local_mail.quota_state("app.sales-snap.com")["daily_used"], 2)
            self.assertEqual(local_mail.submit_local_mail(domain="app.sales-snap.com", sendmail_args=["--", "-t"], data=data), 75)

            local_mail._deliver_message.return_value = (75, 1)
            one = b"From: mailer@app.sales-snap.com\nTo: three@example.com\n\nhello\n"
            self.assertEqual(local_mail.submit_local_mail(domain="app.sales-snap.com", sendmail_args=["--", "-t"], data=one), 75)
            self.assertEqual(local_mail.quota_state("app.sales-snap.com")["daily_used"], 2)

    def test_sendmail_delivery_uses_isolated_listener(self) -> None:
        calls = []

        class FakeSMTP:
            def __init__(self, host, port, timeout):
                calls.append((host, port, timeout))

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def sendmail(self, sender, recipients, data):
                calls.append((sender, tuple(recipients), data))
                return {}

        data = b"From: mailer@app.sales-snap.com\nTo: one@example.com\n\nhello\n"
        with patch.object(local_mail.smtplib, "SMTP", FakeSMTP):
            self.assertEqual(
                local_mail._deliver_message(
                    item={"mta": "sendmail"},
                    domain="app.sales-snap.com",
                    recipients=["one@example.com"],
                    data=data,
                ),
                (0, 0),
            )
        self.assertEqual(calls[0], ("127.0.0.1", 2525, 30))
        self.assertEqual(
            calls[1][2],
            b"From: mailer@app.sales-snap.com\r\nTo: one@example.com\r\n\r\nhello\r\n",
        )

    def test_recipient_parser_prefers_envelope_recipients(self) -> None:
        data = b"To: header@example.com\n\nhello\n"
        self.assertEqual(
            local_mail._message_recipients(["-oi", "--", "Envelope@Example.com"], data),
            ["envelope@example.com"],
        )


if __name__ == "__main__":
    unittest.main()
