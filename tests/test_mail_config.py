from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import mail_config
from mcd_agent.amazon_mailer_dep import AMAZON_MAILER_PACKAGE, HTTP_CLIENT_PACKAGE, SENDGRID_MAILER_PACKAGE


class MailConfigTests(unittest.TestCase):
    def test_success_status_captures_effective_credentials_without_printing_them(self) -> None:
        cfg = SimpleNamespace(mcc_host_name="host-1")
        with patch.object(mail_config, "_mcc_json") as mcc_json, patch.object(
            mail_config,
            "_host_name",
            return_value="host-1",
        ):
            mail_config._profile_status(
                cfg,
                "profile-1",
                status="tested",
                tested=True,
                credentials={"access_key": "ACCESS", "secret_key": "SECRET"},
            )

        payload = mcc_json.call_args.kwargs["payload"]
        self.assertEqual(payload["credentials"], {"access_key": "ACCESS", "secret_key": "SECRET"})

    def test_external_dsn_is_escaped_for_symfony_parameter_bag(self) -> None:
        with patch.object(mail_config, "_local_php") as local_php, patch.object(
            mail_config, "_write_atomic"
        ) as write_atomic, patch.object(mail_config.os, "chown"):
            path = local_php.return_value
            path.read_text.return_value = (
                "<?php return ['parameters'=>['mailer_dsn'=>'smtp://localhost',"
                "'mailer_from_email'=>'old@example.com','mailer_return_path'=>'old@example.com']];"
            )
            path.stat.return_value = SimpleNamespace(st_mode=0o100640, st_uid=33, st_gid=33)
            mail_config._configure_external_mautic(
                "/var/www/app/public_html",
                {"from_email": "new@example.com", "return_path": "bounce@example.com"},
                "smtp://user:p%40ss@example.com:587",
            )

        stored = write_atomic.call_args.args[1]
        self.assertIn("smtp://user:p%%40ss@example.com:587", stored)

    def test_canonical_mcc_host_name_may_differ_from_local_hostname(self) -> None:
        cfg = SimpleNamespace(mcc_host_name="")
        material = {
            "instance_domain": "app.sales-snap.com",
            "current_host_name": "host-203-0-113-10",
            "transport_type": "own_host",
            "settings": {},
            "credentials": {},
            "last_test_recipient": "operator@example.com",
            "name": "Own host",
        }
        with patch.object(mail_config.os, "geteuid", return_value=0), patch.object(
            mail_config, "fetch_mail_profile", return_value=material
        ), patch.object(
            mail_config, "configure_local_mail", return_value={"status": "ok"}
        ) as configure, patch.object(mail_config, "_profile_status"):
            result = mail_config.apply_mail_profile(
                cfg,
                profile_id="profile-1",
                domain="app.sales-snap.com",
                root="/var/www/app/public_html",
            )

        self.assertEqual(result["status"], "ok")
        configure.assert_called_once()

    def test_smtp_dsn_encodes_credentials_and_encryption(self) -> None:
        dsn, packages = mail_config._external_dsn(
            "smtp",
            {"host": "smtp.example.com", "port": 465, "encryption": "tls"},
            {"username": "user@example.com", "password": "p@ss/word"},
        )
        self.assertEqual(dsn, "smtps://user%40example.com:p%40ss%2Fword@smtp.example.com:465")
        self.assertEqual(packages, set())

    def test_ses_api_requires_the_symfony_bridge(self) -> None:
        dsn, packages = mail_config._external_dsn(
            "amazon_ses_api",
            {"region": "eu-central-1", "delivery_method": "ses+api"},
            {"access_key": "access", "secret_key": "secret"},
        )
        self.assertEqual(dsn, "ses+api://access:secret@default?region=eu-central-1")
        self.assertEqual(packages, {AMAZON_MAILER_PACKAGE, HTTP_CLIENT_PACKAGE})

    def test_managed_and_smtp_ses_methods_use_the_selected_dsn_and_credentials(self) -> None:
        managed, managed_packages = mail_config._external_dsn(
            "amazon_ses_api",
            {"region": "eu-central-1", "delivery_method": "mautic+ses+api"},
            {"access_key": "access", "secret_key": "secret"},
        )
        smtp, smtp_packages = mail_config._external_dsn(
            "amazon_ses_api",
            {"region": "eu-central-1", "delivery_method": "ses+smtp"},
            {"smtp_username": "smtp-user", "smtp_password": "smtp-secret"},
        )

        self.assertEqual(managed, "mautic+ses+api://access:secret@default?region=eu-central-1")
        self.assertEqual(managed_packages, {AMAZON_MAILER_PACKAGE, HTTP_CLIENT_PACKAGE})
        self.assertEqual(smtp, "ses+smtp://smtp-user:smtp-secret@default?region=eu-central-1")
        self.assertEqual(smtp_packages, {AMAZON_MAILER_PACKAGE})

    def test_ses_api_method_change_preserves_current_credentials_when_fields_are_blank(self) -> None:
        with patch.object(
            mail_config,
            "_current_mailer_dsn",
            return_value="mautic+ses+api://OLDACCESS:OLDSECRET@default?region=eu-central-1",
        ):
            credentials = mail_config._credentials_for_apply(
                "/var/www/app/public_html",
                "amazon_ses_api",
                {
                    "delivery_method": "ses+api",
                    "_preserve_current_credentials": True,
                },
                {},
            )

        self.assertEqual(credentials, {"access_key": "OLDACCESS", "secret_key": "OLDSECRET"})

    def test_entered_ses_key_replaces_only_that_current_value(self) -> None:
        with patch.object(
            mail_config,
            "_current_mailer_dsn",
            return_value="ses+api://OLDACCESS:OLDSECRET@default?region=eu-central-1",
        ):
            credentials = mail_config._credentials_for_apply(
                "/var/www/app/public_html",
                "amazon_ses_api",
                {
                    "delivery_method": "ses+https",
                    "_preserve_current_credentials": True,
                },
                {"access_key": "NEWACCESS"},
            )

        self.assertEqual(credentials, {"access_key": "NEWACCESS", "secret_key": "OLDSECRET"})

    def test_ses_api_credentials_are_not_reused_for_ses_smtp(self) -> None:
        with patch.object(
            mail_config,
            "_current_mailer_dsn",
            return_value="ses+api://ACCESS:SECRET@default?region=eu-central-1",
        ):
            credentials = mail_config._credentials_for_apply(
                "/var/www/app/public_html",
                "amazon_ses_api",
                {
                    "delivery_method": "ses+smtp",
                    "_preserve_current_credentials": True,
                },
                {},
            )

        self.assertEqual(credentials, {})

    def test_sendgrid_api_requires_the_symfony_bridge(self) -> None:
        dsn, packages = mail_config._external_dsn(
            "sendgrid_api",
            {},
            {"api_key": "SG.secret"},
        )
        self.assertEqual(dsn, "sendgrid+api://SG.secret@default")
        self.assertEqual(packages, {SENDGRID_MAILER_PACKAGE})


if __name__ == "__main__":
    unittest.main()
