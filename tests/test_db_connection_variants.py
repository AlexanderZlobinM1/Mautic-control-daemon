from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcd_agent.db import MauticDB
from mcd_agent.models import DBConfig


class MauticDBConnectionVariantsTest(unittest.TestCase):
    def _db(self, host: str) -> MauticDB:
        return MauticDB(
            DBConfig(
                host=host,
                port=3306,
                name="baza_ss",
                user="korisnik_ss",
                password="secret",
                table_prefix="ss_",
            )
        )

    def test_implicit_localhost_prefers_unix_socket_before_tcp_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sock = Path(td) / "mysqld.sock"
            sock.touch()
            db = self._db("localhost")

            with (
                patch.object(MauticDB, "_socket_candidates", return_value=[str(sock)]),
            ):
                variants = db._connect_variants()

        self.assertEqual(variants[0]["unix_socket"], str(sock))
        self.assertEqual(variants[1]["host"], "localhost")
        self.assertEqual(variants[2]["host"], "127.0.0.1")

    def test_explicit_tcp_loopback_keeps_tcp_before_socket_fallback(self) -> None:
        db = self._db("127.0.0.1")

        with patch.object(MauticDB, "_socket_candidates", return_value=["/run/mysqld/mysqld.sock"]):
            variants = db._connect_variants()

        self.assertEqual(variants[0]["host"], "127.0.0.1")
        self.assertEqual(variants[1]["unix_socket"], "/run/mysqld/mysqld.sock")


if __name__ == "__main__":
    unittest.main()
