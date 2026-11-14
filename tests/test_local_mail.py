from __future__ import annotations

import json
import inspect
from pathlib import Path
import re
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcd_agent import local_mail


class LocalMailTests(unittest.TestCase):
    def test_mail_os_operation_uses_existing_generic_binding_and_typed_result(self) -> None:
        cfg = SimpleNamespace()
        runtime = {
            "mta": "postfix",
            "mail_hostname": "mail.example.test",
            "domains": ["example.test"],
            "selector": "mcd",
        }
        with patch.object(
            local_mail,
            "run_configured_external_operation",
            return_value=(0, '{"status":"applied","changed_files":[]}'),
        ) as dispatch:
            result = local_mail._configured_mail_os_operation(
                cfg,
                operation="mail_os_filesystem_apply",
                runtime=runtime,
            )
        self.assertEqual(result, {"status": "applied", "changed_files": []})
        dispatch.assert_called_once_with(
            cfg,
            component="mail",
            operation="mail_os_filesystem_apply",
            profile={},
            operation_input=runtime,
        )

    def test_mail_os_operation_fails_closed_when_binding_is_missing(self) -> None:
        with patch.object(
            local_mail,
            "run_configured_external_operation",
            return_value=(2, "custom_operation_binding_not_configured"),
        ):
            self.assertIsNone(
                local_mail._configured_mail_os_operation(
                    SimpleNamespace(),
                    operation="mail_os_filesystem_cleanup",
                    runtime={"mta": "postfix", "mail_hostname": "mail.example.test", "domains": [], "selector": "mcd"},
                )
            )

    def test_mail_submit_capability_is_typed_and_legacy_key_path_is_migrated(self) -> None:
        self.assertEqual(
            local_mail._resolved_submit_command({"status": "applied", "submit_command": "mcd-test-submit"}),
            "mcd-test-submit",
        )
        for value in (None, "", "mcd-test\nsubmit"):
            with self.assertRaisesRegex(RuntimeError, "mail_os_submit_interface_command_invalid"):
                local_mail._resolved_submit_command({"submit_command": value})
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "domains.json"
            with patch.object(local_mail, "DOMAINS_PATH", state_path):
                local_mail._save_domains({
                    "schema": 1,
                    "domains": {"example.test": {"key_path": "/etc/opendkim/keys/example.test/mcd.private", "root": td}},
                })
            saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotIn("key_path", saved["domains"]["example.test"])

    def test_mail_os_concrete_host_operations_are_not_implemented_in_mcd(self) -> None:
        source = Path(local_mail.__file__).read_text(encoding="utf-8")

        for residue in (
            "apt-get",
            "dpkg-query",
            "iptables",
            "systemctl",
            "postconf",
            "postmap",
            "makemap",
            "opendkim-testkey",
            "_configure_opendkim",
            "_configure_mta",
            "_configure_inbound",
            "_configure_smtp_firewall",
            "OPENDKIM_KEYS",
            "SUBMIT_WRAPPER",
            "SUBMIT_ROOT_HELPER",
            "SUBMIT_SUDOERS",
            "_write_key",
            "_write_submit_wrapper",
            "visudo",
            "/usr/local/",
            "/etc/opendkim/",
        ):
            self.assertNotIn(residue, source)

        self.assertIn("mail_os_filesystem_apply", source)
        self.assertIn("mail_os_filesystem_cleanup", source)
        self.assertIn("mail_os_lifecycle_apply", source)
        self.assertIn("mail_os_lifecycle_cleanup", source)
        self.assertIn("mail_os_lifecycle_status", source)
        self.assertIn("mail_os_opendkim_key_apply", source)
        self.assertIn("mail_os_opendkim_key_cleanup", source)
        self.assertIn("mail_os_opendkim_key_status", source)
        self.assertIn("mail_os_submit_interface_apply", source)
        self.assertIn("mail_os_submit_interface_cleanup", source)
        self.assertIn("mail_os_submit_interface_status", source)

    def test_mail_cleanup_disables_lifecycle_before_removing_external_files(self) -> None:
        configure_source = inspect.getsource(local_mail.configure_local_mail)
        rollback = configure_source[configure_source.index("except Exception as exc:"):]
        self.assertLess(
            rollback.index('operation="mail_os_lifecycle_cleanup"'),
            rollback.index('operation="mail_os_submit_interface_cleanup"'),
        )
        self.assertLess(
            rollback.index('operation="mail_os_submit_interface_cleanup"'),
            rollback.index('operation="mail_os_filesystem_cleanup"'),
        )
        self.assertLess(
            rollback.index('operation="mail_os_filesystem_cleanup"'),
            rollback.index('operation="mail_os_opendkim_key_cleanup"'),
        )

        disable_source = inspect.getsource(local_mail.disable_local_mail)
        self.assertLess(
            disable_source.index('operation="mail_os_lifecycle_cleanup"'),
            disable_source.index('operation="mail_os_submit_interface_cleanup"'),
        )
        self.assertLess(
            disable_source.index('operation="mail_os_submit_interface_cleanup"'),
            disable_source.index('operation="mail_os_filesystem_cleanup"'),
        )
        self.assertLess(
            disable_source.rindex('operation="mail_os_filesystem_cleanup"'),
            disable_source.rindex('operation="mail_os_opendkim_key_cleanup"'),
        )

    def test_mail_lifecycle_apply_failure_rolls_back_external_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            local_php = Path(td) / "local.php"
            local_php.write_text("<?php return [];\n", encoding="utf-8")
            calls: list[str] = []

            def external(_cfg, *, operation, runtime):
                del runtime
                calls.append(operation)
                return {
                    "mail_os_filesystem_apply": {"status": "applied"},
                    "mail_os_opendkim_key_apply": {"status": "applied"},
                    "mail_os_submit_interface_apply": {"status": "applied", "submit_command": "mcd-test-submit"},
                    "mail_os_lifecycle_apply": {"status": "error", "error": "mocked"},
                    "mail_os_lifecycle_cleanup": {"status": "cleaned"},
                    "mail_os_submit_interface_cleanup": {"status": "cleaned"},
                    "mail_os_filesystem_cleanup": {"status": "cleaned"},
                    "mail_os_opendkim_key_cleanup": {"status": "cleaned"},
                }[operation]

            with patch.object(local_mail.os, "geteuid", return_value=0), patch.object(
                local_mail,
                "_preflight_local_mail_material",
                return_value=(
                    {
                        "private_key_pem": "test-key",
                        "mail_hostname": "mail.example.test",
                        "selector": "mcd",
                    },
                    "mail.example.test",
                    {},
                ),
            ), patch.object(local_mail, "_local_php", return_value=local_php), patch.object(
                local_mail,
                "_load_domains",
                return_value={"schema": 1, "domains": {}},
            ), patch.object(local_mail, "_save_domains"), patch.object(
                local_mail, "_configured_mail_os_operation", side_effect=external
            ):
                with self.assertRaisesRegex(RuntimeError, "mail_os_lifecycle_apply_failed"):
                    local_mail.configure_local_mail(
                        SimpleNamespace(),
                        domain="example.test",
                        root=td,
                        validate_runtime=False,
                    )

            self.assertEqual(
                calls,
                [
                    "mail_os_filesystem_apply",
                    "mail_os_opendkim_key_apply",
                    "mail_os_submit_interface_apply",
                    "mail_os_lifecycle_apply",
                    "mail_os_lifecycle_cleanup",
                    "mail_os_submit_interface_cleanup",
                    "mail_os_filesystem_cleanup",
                    "mail_os_opendkim_key_cleanup",
                ],
            )

    def test_mail_status_requires_all_external_read_only_probes(self) -> None:
        calls: list[str] = []

        def external(_cfg, *, operation, runtime):
            calls.append(operation)
            self.assertNotIn("private_key_pem", runtime)
            return {
                "mail_os_lifecycle_status": {"status": "ok", "read_only": True, "services": {}},
                "mail_os_opendkim_key_status": {"status": "ok", "read_only": True, "observed": {}},
                "mail_os_submit_interface_status": {"status": "ok", "read_only": True, "observed": {}},
            }[operation]

        with patch.object(local_mail, "_load_domains", return_value={
            "schema": 1,
            "mail_identity_hostname": "mail.example.test",
            "domains": {
                "example.test": {"mail_hostname": "mail.example.test", "selector": "mcd", "mta": "postfix"}
            },
        }), patch.object(local_mail, "quota_state", return_value={}), patch.object(
            local_mail, "_mail_identity", return_value={"fcrdns": True}
        ), patch.object(local_mail, "_configured_mail_os_operation", side_effect=external):
            result = local_mail.local_mail_status(SimpleNamespace(), domain="example.test")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, [
            "mail_os_lifecycle_status",
            "mail_os_opendkim_key_status",
            "mail_os_submit_interface_status",
        ])
        self.assertEqual(result["opendkim_key"]["read_only"], True)
        self.assertEqual(result["submit_interface"]["read_only"], True)

    def test_runtime_keeps_python_310_compatible_utc_import(self) -> None:
        source = Path(local_mail.__file__).read_text(encoding="utf-8")

        self.assertNotIn("from datetime import UTC", source)
        self.assertIn("datetime.now(timezone.utc)", source)

    def test_mail_test_supports_returned_and_assignment_style_local_php(self) -> None:
        script = local_mail._MAIL_TEST_SCRIPT
        self.assertIn("$included = include $argv[1]", script)
        self.assertIn("is_array($included)", script)
        self.assertIn("isset($parameters) && is_array($parameters)", script)

    def test_mail_identity_requires_forward_confirmed_reverse_dns(self) -> None:
        with patch.object(local_mail.shutil, "which", return_value="/usr/bin/dig"), patch.object(
            local_mail,
            "_run",
            side_effect=[(0, "46.62.129.237\n"), (0, "mail.nikola.sales-snap.com.\n")],
        ):
            result = local_mail._mail_identity("mail.nikola.sales-snap.com")

        self.assertTrue(result["fcrdns"])
        self.assertEqual(result["ipv4"], ["46.62.129.237"])
        self.assertEqual(result["ptr"]["46.62.129.237"], "mail.nikola.sales-snap.com")
        self.assertEqual(result["error"], "")

    def test_mail_identity_reports_mismatched_ptr(self) -> None:
        with patch.object(local_mail.shutil, "which", return_value="/usr/bin/dig"), patch.object(
            local_mail,
            "_run",
            side_effect=[(0, "46.62.129.237\n"), (0, "static.example.net.\n")],
        ):
            result = local_mail._mail_identity("mail.nikola.sales-snap.com")

        self.assertFalse(result["fcrdns"])
        self.assertIn("expected mail.nikola.sales-snap.com", result["error"])

    def test_local_mail_preflight_uses_shared_identity_from_mcc(self) -> None:
        material = {
            "enabled": True,
            "mail_hostname": "mail.farm03.sales-snap.com",
            "private_key_pem": "not-returned",
        }
        identity = {"hostname": "mail.farm03.sales-snap.com", "fcrdns": True, "error": ""}
        with patch.object(local_mail, "fetch_material", return_value=material), patch.object(
            local_mail, "_mail_identity", return_value=identity
        ):
            result = local_mail.preflight_local_mail(SimpleNamespace(), domain="vida.sales-snap.com")

        self.assertEqual(result["mail_identity_hostname"], "mail.farm03.sales-snap.com")
        self.assertNotIn("private_key_pem", result)

    def test_local_mail_preflight_rejects_mismatched_shared_identity(self) -> None:
        with patch.object(
            local_mail,
            "fetch_material",
            return_value={"enabled": True, "mail_hostname": "mail.farm03.sales-snap.com"},
        ), patch.object(
            local_mail,
            "_mail_identity",
            return_value={"fcrdns": False, "error": "PTR mismatch"},
        ):
            with self.assertRaisesRegex(RuntimeError, "PTR mismatch"):
                local_mail.preflight_local_mail(SimpleNamespace(), domain="vida.sales-snap.com")

    def test_public_dns_short_ignores_local_resolver_when_public_dns_answers(self) -> None:
        with patch.object(
            local_mail,
            "_run",
            side_effect=[(0, "mail.nikola.sales-snap.com.\n")],
        ) as run:
            rc, out = local_mail._public_dns_short(
                "/usr/bin/dig", "-x", "46.62.129.237"
            )

        self.assertEqual(rc, 0)
        self.assertEqual(out, "mail.nikola.sales-snap.com.\n")
        run.assert_called_once_with(
            ["/usr/bin/dig", "@1.1.1.1", "+short", "-x", "46.62.129.237"],
            timeout_sec=15,
        )

    def test_mautic_patch_uses_quota_wrapper_and_instance_from_domain(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "local.php"
            path.write_text(
                "<?php return ['parameters'=>['mailer_dsn'=>'smtp://localhost:25','mailer_from_email'=>'hello@example.com','mailer_return_path'=>null]];\n",
                encoding="utf-8",
            )
            with patch.object(local_mail, "_local_php", return_value=path):
                local_mail._configure_mautic(
                    "/var/www/app/public_html", "app.sales-snap.com", submit_command="mcd-test-submit"
                )
            result = path.read_text(encoding="utf-8")
            self.assertIn(
                "mcd-test-submit%%20--instance-domain%%3Dapp.sales-snap.com%%20--%%20-oi%%20-t",
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
                local_mail._configure_mautic(
                    "/var/www/app/public_html", "app.sales-snap.com", submit_command="mcd-test-submit"
                )
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
                local_mail._configure_mautic(
                    "/var/www/app/public_html", "app.sales-snap.com", submit_command="mcd-test-submit"
                )
            result = path.read_text(encoding="utf-8")

        self.assertIn("$parameters = array(\n    'mailer_return_path'", result)
        self.assertIn("'mailer_dsn' =>", result)
        self.assertIn("'mailer_from_email' => 'mailer@app.sales-snap.com'", result)
        self.assertIn("'db_driver' => 'pdo_mysql'", result)

    def test_mautic_patch_rejects_unsupported_existing_mail_parameter(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsupported value: mailer_dsn"):
            local_mail._set_php_parameter("<?php return ['parameters'=>['mailer_dsn'=>[]]];", "mailer_dsn", "x")

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
