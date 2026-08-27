from __future__ import annotations

import unittest
from unittest.mock import patch

from mcd_agent.mautic_db_indexes import (
    MANAGED_INDEXES,
    _add_index_sql,
    apply_mautic_db_indexes_to_install,
    _connect,
    _fax_indexes_to_drop,
    _index_already_present,
    _is_duplicate_key_error,
    _is_too_many_indexes_error,
)
import pymysql
from mcd_agent.models import DBConfig, MauticInstall


class MauticDbIndexesTests(unittest.TestCase):
    def test_index_connection_allows_long_online_ddl(self) -> None:
        db = DBConfig(host="localhost", port=3306, name="demo", user="u", password="p", table_prefix="ss_")

        with patch("mcd_agent.mautic_db_indexes.pymysql.connect") as connect:
            _connect(db)

        self.assertEqual(connect.call_args.kwargs["read_timeout"], 7200)

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
        self.assertEqual(
            by_name["idx_mcd_ph_lead_date"].columns,
            ("lead_id", "date_hit", "id"),
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

    def test_duplicate_key_error_is_detected_for_index_races(self) -> None:
        self.assertTrue(_is_duplicate_key_error(pymysql.err.OperationalError(1061, "Duplicate key name 'idx_mcd_audit_segment_due'")))

    def test_duplicate_key_race_refreshes_indexes_and_skips_existing_index(self) -> None:
        idx = MANAGED_INDEXES[0]

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, *_args):
                if "ADD INDEX" in str(sql):
                    raise pymysql.err.OperationalError(1061, f"Duplicate key name '{idx.name}'")

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def cursor(self):
                return Cursor()

        install = MauticInstall(
            instance_uid="demo",
            name="demo",
            root="/var/www/demo",
            console_path="/var/www/demo/bin/console",
            db=DBConfig(host="localhost", port=3306, name="demo", user="u", password="p", table_prefix="ss_"),
        )

        import mcd_agent.mautic_db_indexes as mod

        calls = 0
        old_connect = mod._connect
        old_existing = mod._existing_indexes
        old_managed = mod.MANAGED_INDEXES
        try:
            mod.MANAGED_INDEXES = (idx,)
            mod._connect = lambda _db: Conn()

            def fake_existing(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                return {} if calls == 1 else {idx.name: idx.columns}

            mod._existing_indexes = fake_existing
            result = apply_mautic_db_indexes_to_install(install)
        finally:
            mod._connect = old_connect
            mod._existing_indexes = old_existing
            mod.MANAGED_INDEXES = old_managed

        self.assertEqual(result["status"], "noop")
        self.assertEqual(result["skipped"], [{"index": idx.name, "reason": "already_created:name_match"}])

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
