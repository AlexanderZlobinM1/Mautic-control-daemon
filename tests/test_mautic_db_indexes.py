from __future__ import annotations

import unittest

from mcd_agent.mautic_db_indexes import (
    MANAGED_INDEXES,
    _add_index_sql,
    _connect,
    _fax_indexes_to_drop,
    _index_already_present,
    _is_too_many_indexes_error,
)
import pymysql
from mcd_agent.models import DBConfig


class MauticDbIndexesTests(unittest.TestCase):
    def test_managed_index_sql_uses_prefix_and_online_ddl(self) -> None:
        sql = _add_index_sql("ss_", MANAGED_INDEXES[0])

        self.assertIn("ALTER TABLE `ss_lead_lists_leads`", sql)
        self.assertIn("`idx_mcd_lll_list_date_removed_lead`", sql)
        self.assertIn("`leadlist_id`, `date_added`, `manually_removed`, `lead_id`", sql)
        self.assertIn("ALGORITHM=INPLACE, LOCK=NONE", sql)

    def test_global_profile_contains_email_mobile_and_campaign_schedule_indexes(self) -> None:
        by_name = {idx.name: idx for idx in MANAGED_INDEXES}

        self.assertEqual(by_name["idx_mcd_leads_email"].columns, ("email",))
        self.assertEqual(by_name["idx_mcd_leads_mobile"].columns, ("mobile",))
        self.assertEqual(
            by_name["idx_mcd_clel_scheduled_trigger_id"].columns,
            ("is_scheduled", "trigger_date", "id"),
        )

    def test_fax_indexes_are_prunable_when_mysql_index_limit_is_hit(self) -> None:
        indexes = {
            "PRIMARY": ("id",),
            "fax": ("fax",),
            "idx_contacts_fax": ("some_other_column",),
            "idx_email": ("email",),
        }

        self.assertEqual(_fax_indexes_to_drop(indexes), ["fax", "idx_contacts_fax"])
        self.assertTrue(_is_too_many_indexes_error(pymysql.err.OperationalError(1069, "Too many keys specified; max 64 keys allowed")))

    def test_existing_index_detected_by_same_columns_under_other_name(self) -> None:
        present, reason = _index_already_present(
            {"custom_idx": ("leadlist_id", "date_added", "manually_removed", "lead_id")},
            MANAGED_INDEXES[0],
        )

        self.assertTrue(present)
        self.assertEqual(reason, "columns_match:custom_idx")

    def test_localhost_connection_prefers_unix_socket(self) -> None:
        captured = {}

        def fake_connect(**kwargs):
            captured.update(kwargs)
            class Dummy:
                pass
            return Dummy()

        import mcd_agent.mautic_db_indexes as mod

        old_exists = mod.os.path.exists
        old_connect = mod.pymysql.connect
        try:
            mod.os.path.exists = lambda path: path == "/run/mysqld/mysqld.sock"
            mod.pymysql.connect = fake_connect
            _connect(DBConfig(host="localhost", port=3306, name="db", user="u", password="p", table_prefix=""))
        finally:
            mod.os.path.exists = old_exists
            mod.pymysql.connect = old_connect

        self.assertEqual(captured.get("unix_socket"), "/run/mysqld/mysqld.sock")
        self.assertNotIn("host", captured)


if __name__ == "__main__":
    unittest.main()
