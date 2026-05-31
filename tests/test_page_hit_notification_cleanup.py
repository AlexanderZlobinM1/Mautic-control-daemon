from __future__ import annotations

import unittest

from mcd_agent.db import MauticDB


class PageHitNotificationCleanupTests(unittest.TestCase):
    def test_extracts_private_php_serialized_hit_id(self) -> None:
        body = (
            'O:58:"Mautic\\MessengerBundle\\Message\\PageHitNotification":1:{'
            's:65:"\x00Mautic\\MessengerBundle\\Message\\PageHitNotification\x00hitId";i:123;'
            "}"
        )

        self.assertEqual(MauticDB.extract_page_hit_notification_hit_id(body), 123)

    def test_extracts_json_hit_id(self) -> None:
        body = '{"type":"Mautic\\\\MessengerBundle\\\\Message\\\\PageHitNotification","hitId":456}'

        self.assertEqual(MauticDB.extract_page_hit_notification_hit_id(body), 456)

    def test_ignores_other_message_types(self) -> None:
        self.assertIsNone(MauticDB.extract_page_hit_notification_hit_id('{"hitId":456}'))


if __name__ == "__main__":
    unittest.main()
