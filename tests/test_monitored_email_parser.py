import unittest

from mcd_agent.monitored_email import (
    DNC_BOUNCED,
    DNC_UNSUBSCRIBED,
    MonitoredEmailParserSettings,
    TYPE_BOUNCE,
    TYPE_FEEDBACK_LOOP,
    TYPE_UNSUBSCRIBE,
    monitored_email_targets,
    parse_monitored_message,
    process_monitored_email,
)


class MonitoredEmailParserTests(unittest.TestCase):
    def test_feedback_loop_extracts_recipient_from_embedded_unsubscribe_url(self):
        raw = (
            b"From: fbl@example.net\r\n"
            b"To: abuse@example.com\r\n"
            b"Subject: complaint\r\n"
            b"Content-Type: multipart/report; report-type=feedback-report; boundary=outer\r\n"
            b"\r\n"
            b"--outer\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"Spam complaint\r\n"
            b"--outer\r\n"
            b"Content-Type: message/feedback-report\r\n"
            b"\r\n"
            b"Feedback-Type: abuse\r\n"
            b"User-Agent: Mail.Ru FBL\r\n"
            b"\r\n"
            b"--outer\r\n"
            b"Content-Type: message/rfc822\r\n"
            b"\r\n"
            b"From: sender@example.com\r\n"
            b"To: client@example.org\r\n"
            b"List-Unsubscribe: <https://mautic.example.com/email/unsubscribe/client@example.org/token>\r\n"
            b"\r\n"
            b"Campaign body\r\n"
            b"--outer--\r\n"
        )

        parsed = parse_monitored_message(raw, (TYPE_FEEDBACK_LOOP,))

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.kind, TYPE_FEEDBACK_LOOP)
        self.assertEqual(parsed.reason, DNC_UNSUBSCRIBED)
        self.assertEqual(parsed.emails, ["client@example.org"])

    def test_bounce_extracts_final_recipient_from_delivery_status(self):
        raw = (
            b"From: MAILER-DAEMON@example.net\r\n"
            b"To: bounce@example.com\r\n"
            b"Subject: Delivery failed\r\n"
            b"Content-Type: multipart/report; report-type=delivery-status; boundary=outer\r\n"
            b"\r\n"
            b"--outer\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"Delivery failed\r\n"
            b"--outer\r\n"
            b"Content-Type: message/delivery-status\r\n"
            b"\r\n"
            b"Final-Recipient: rfc822; bounced@example.org\r\n"
            b"Action: failed\r\n"
            b"Status: 5.1.1\r\n"
            b"--outer--\r\n"
        )

        parsed = parse_monitored_message(raw, (TYPE_BOUNCE,))

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.kind, TYPE_BOUNCE)
        self.assertEqual(parsed.reason, DNC_BOUNCED)
        self.assertEqual(parsed.emails, ["bounced@example.org"])

    def test_unsubscribe_extracts_sender_for_direct_unsubscribe_request(self):
        raw = (
            b"From: Customer <customer@example.org>\r\n"
            b"To: unsubscribe@example.com\r\n"
            b"Subject: Please unsubscribe me\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Unsubscribe please\r\n"
        )

        parsed = parse_monitored_message(raw, (TYPE_UNSUBSCRIBE,))

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.kind, TYPE_UNSUBSCRIBE)
        self.assertEqual(parsed.reason, DNC_UNSUBSCRIBED)
        self.assertEqual(parsed.emails, ["customer@example.org"])

    def test_whitelist_filters_exact_forwarder_email(self):
        raw = (
            b"From: Internal <support@example.org>\r\n"
            b"To: unsubscribe@example.com\r\n"
            b"Subject: Please unsubscribe me\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Unsubscribe please\r\n"
        )

        parsed = parse_monitored_message(raw, (TYPE_UNSUBSCRIBE,), ["support@example.org"])

        self.assertIsNone(parsed)

    def test_targets_reuse_mautic_monitored_mailboxes_by_type(self):
        monitored = {
            "general": {
                "host": "imap.example.com",
                "port": "993",
                "encryption": "/ssl",
                "user": "mailbox@example.com",
                "password": "secret",
            },
            "EmailBundle_unsubscribes": {"folder": "FBL"},
            "EmailBundle_bounces": {"folder": "Bounces"},
        }

        targets = monitored_email_targets(monitored, (TYPE_FEEDBACK_LOOP, TYPE_BOUNCE, TYPE_UNSUBSCRIBE))

        self.assertEqual([target.folder for target in targets], ["FBL", "Bounces"])
        self.assertEqual([target.role for target in targets], [TYPE_FEEDBACK_LOOP, TYPE_BOUNCE])

    def test_process_removes_existing_email_dnc_for_whitelist(self):
        class FakeDB:
            removed = []

            def remove_email_dnc_for_emails(self, emails):
                self.removed = list(emails)
                return {"removed": len(emails)}

        db = FakeDB()

        result = process_monitored_email(
            db=db,
            local_php_path=None,
            php_bin="/usr/bin/php",
            settings=MonitoredEmailParserSettings(enabled=True, whitelist=("support@example.org",)),
            state={},
        )

        self.assertEqual(db.removed, ["support@example.org"])
        self.assertEqual(result.whitelist_dnc_removed, 1)


if __name__ == "__main__":
    unittest.main()
