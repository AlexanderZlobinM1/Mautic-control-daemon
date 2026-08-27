from __future__ import annotations

import unittest
from unittest.mock import Mock

from mcd_agent.db import MauticDB
from mcd_agent.models import DBConfig


class _Cursor:
    def __init__(self, acquired: int) -> None:
        self.acquired = acquired
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.executed.append((str(sql), tuple(params)))

    def fetchone(self):
        return {"acquired": self.acquired}


class _Connection:
    def __init__(self, acquired: int) -> None:
        self.cur = _Cursor(acquired)
        self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


class PageHitsCleanupLockTests(unittest.TestCase):
    @staticmethod
    def _db() -> MauticDB:
        return MauticDB(DBConfig(host="localhost", port=3306, name="baza_ss", user="u", password="p", table_prefix="ss_"))

    def test_acquired_lock_stays_open_until_release(self) -> None:
        db = self._db()
        conn = _Connection(1)
        db._connect = lambda: conn  # type: ignore[method-assign]

        token = db.try_acquire_orphan_page_hits_cleanup_lock()

        self.assertIs(token, conn)
        self.assertFalse(conn.closed)
        self.assertEqual(conn.cur.executed[0][1], ("mcd:page_hits_orphan:baza_ss",))
        db.release_orphan_page_hits_cleanup_lock(token)
        self.assertTrue(conn.closed)

    def test_busy_lock_closes_probe_connection(self) -> None:
        db = self._db()
        conn = _Connection(0)
        db._connect = lambda: conn  # type: ignore[method-assign]

        token = db.try_acquire_orphan_page_hits_cleanup_lock()

        self.assertIsNone(token)
        self.assertTrue(conn.closed)

    def test_cleanup_requires_managed_page_hits_index(self) -> None:
        db = self._db()
        check = Mock(return_value=True)
        db._table_has_index = check  # type: ignore[method-assign]

        self.assertTrue(db.orphan_page_hits_cleanup_index_ready())
        check.assert_called_once_with("ss_page_hits", "idx_mcd_ph_lead_date")

    def test_cleanup_is_not_ready_without_managed_page_hits_index(self) -> None:
        db = self._db()
        db._table_has_index = Mock(return_value=False)  # type: ignore[method-assign]

        self.assertFalse(db.orphan_page_hits_cleanup_index_ready())


if __name__ == "__main__":
    unittest.main()
