import unittest
from unittest.mock import patch

from mcd_agent.db import MauticDB
from mcd_agent.models import DBConfig


class _FakeCursor:
    def __init__(self):
        self.leads = []
        self.dnc = []
        self._rows = []
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        normalized = " ".join(str(query).split())
        self._rows = []
        self._one = None
        if normalized.startswith("SELECT `id`, LOWER(`email`) AS email"):
            wanted = {str(value).lower() for value in params}
            self._rows = [row.copy() for row in self.leads if row["email"] in wanted]
        elif normalized.startswith("SELECT GET_LOCK"):
            self._one = {"acquired": 1}
        elif normalized.startswith("SELECT `id` FROM `ss_leads` WHERE LOWER(`email`)"):
            email = str(params[0]).lower()
            row = next((item for item in self.leads if item["email"] == email), None)
            self._one = {"id": row["id"]} if row else None
        elif normalized.startswith("SHOW COLUMNS FROM `ss_leads`"):
            self._rows = [
                {"Field": name}
                for name in ("id", "email", "date_added", "date_identified", "is_published", "points")
            ]
        elif normalized.startswith("INSERT INTO `ss_leads`"):
            self.leads.append({"id": len(self.leads) + 1, "email": str(params[0]).lower()})
        elif normalized.startswith("SELECT RELEASE_LOCK"):
            self._one = {"released": 1}
        elif normalized.startswith("SELECT `lead_id`, `reason` FROM `ss_lead_donotcontact`"):
            wanted = {int(value) for value in params}
            self._rows = [row.copy() for row in self.dnc if row["lead_id"] in wanted]
        else:
            raise AssertionError(normalized)
        return 1

    def executemany(self, query, rows):
        normalized = " ".join(str(query).split())
        if not normalized.startswith("INSERT INTO `ss_lead_donotcontact`"):
            raise AssertionError(normalized)
        for lead_id, _date_added, reason, _comments in rows:
            self.dnc.append({"lead_id": int(lead_id), "reason": int(reason)})
        return len(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._one


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


class MonitoredEmailDBTests(unittest.TestCase):
    def test_create_missing_contact_and_dnc_is_idempotent(self):
        db = MauticDB(
            DBConfig(
                host="localhost", port=3306, name="mautic", user="fixture",
                password="fixture", table_prefix="ss_",
            )
        )
        cursor = _FakeCursor()
        connection = _FakeConnection(cursor)
        with patch.object(db, "_connect", return_value=connection):
            first = db.add_dnc_for_emails(
                ["New.Contact@example.org"],
                reason=1,
                comments="User unsubscribed.",
                create_missing=True,
            )
            second = db.add_dnc_for_emails(
                ["new.contact@example.org"],
                reason=1,
                comments="User unsubscribed.",
                create_missing=True,
            )
            different_reason = db.add_dnc_for_emails(
                ["new.contact@example.org"],
                reason=3,
                comments="Bounce",
                create_missing=True,
            )

        self.assertEqual(
            {key: first[key] for key in ("emails", "matched_emails", "contacts", "created", "added")},
            {"emails": 1, "matched_emails": 1, "contacts": 1, "created": 1, "added": 1},
        )
        self.assertEqual((second["created"], second["added"], second["matching_existing"]), (0, 0, 1))
        self.assertEqual(
            (different_reason["created"], different_reason["added"], different_reason["existing"], different_reason["matching_existing"]),
            (0, 0, 1, 0),
        )
        self.assertEqual(cursor.leads, [{"id": 1, "email": "new.contact@example.org"}])
        self.assertEqual(cursor.dnc, [{"lead_id": 1, "reason": 1}])


if __name__ == "__main__":
    unittest.main()
