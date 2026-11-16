from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mcd_agent import plugin_registration
from mcd_agent.db import MauticDB
from mcd_agent.plugins import _normalize_action


def database():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.rowcount = 1
    db = SimpleNamespace(cfg=SimpleNamespace(table_prefix='fixture_'), _safe_table=MauticDB._safe_table,
                         _connect=MagicMock(return_value=conn))
    conn.__enter__.return_value = conn
    return db, conn, cur


def test_remove_only_detaches_registration_and_never_reads_values():
    db, conn, cur = database()
    plugin_registration.unregister(db, ['DemoBundle', 'UnrelatedBundle'])
    queries = '\n'.join(c.args[0] for c in cur.execute.call_args_list)
    assert 'SET i.plugin_id=NULL' in queries
    assert 'DELETE i FROM' not in queries
    assert 'api_keys' not in queries and 'is_published' not in queries and 'supported_features' not in queries
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_purge_removes_only_selected_retained_settings():
    db, conn, cur = database()
    plugin_registration.unregister(db, ['DemoBundle'], purge=True)
    calls = [c for c in cur.execute.call_args_list if 'DELETE i FROM' in c.args[0]]
    assert len(calls) == 1 and calls[0].args[1] == ['DemoBundle', 'DemoBundle']
    assert 'i.plugin_id IS NULL OR p.bundle IN' in calls[0].args[0]
    conn.commit.assert_called_once()


def test_failure_rolls_back_before_settings_can_be_cascaded():
    db, conn, cur = database()
    def execute(query, *args):
        if 'SET i.plugin_id=NULL' in query:
            raise RuntimeError('database failure')
    cur.execute.side_effect = execute
    with pytest.raises(RuntimeError, match='database failure'):
        plugin_registration.unregister(db, ['DemoBundle'])
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
    assert not any('DELETE FROM `fixture_plugins`' in c.args[0] for c in cur.execute.call_args_list)


def test_reinstall_restores_only_orphan_registration_references():
    db, conn, cur = database()
    plugin_registration.restore(db, {'DemoBundle'})
    query, names = cur.execute.call_args.args
    assert 'SET i.plugin_id=p.id WHERE i.plugin_id IS NULL' in query
    assert 'ON BINARY p.bundle=BINARY j.bundle' in query
    assert names == ['DemoBundle']
    assert 'api_keys' not in query


def test_purge_is_distinct_explicit_action():
    assert _normalize_action('remove') == 'remove'
    assert _normalize_action('purge') == 'purge'
    assert _normalize_action('') == 'auto'


def test_cluster_missing_journal_never_issues_ddl_or_deletes_registration():
    db, conn, cur = database()
    cur.fetchone.side_effect = [(1,), None]
    with pytest.raises(RuntimeError, match='approved maintenance'):
        plugin_registration.unregister(db, ['DemoBundle'], allow_schema_creation=False)
    assert not any('CREATE' in c.args[0] or 'DELETE' in c.args[0] for c in cur.execute.call_args_list)


def test_absent_conflicting_registration_requires_no_schema_change():
    db, conn, cur = database()
    cur.fetchone.return_value = None
    assert plugin_registration.unregister(db, ['AbsentBundle'], allow_schema_creation=False) == 0
    assert len(cur.execute.call_args_list) == 1
    conn.begin.assert_not_called()


def test_restore_without_journal_is_read_only():
    db, conn, cur = database()
    cur.fetchone.return_value = None
    assert plugin_registration.restore(db, {'DemoBundle'}) == 0
    assert len(cur.execute.call_args_list) == 1
