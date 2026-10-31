from types import SimpleNamespace

import pytest

from mcd_agent import inventory as module
from mcd_agent.inventory import InstanceInventory
from mcd_agent.models import MauticInstall


def register(inv, uid, root, php, user):
    inv.add_or_update_manual(name=uid, root=root, console_path=root+'/bin/console',
        local_php_path=None, mautic_major=7, db_host=None, db_port=None,
        db_name=None, db_user=None, db_password=None, db_table_prefix=None,
        instance_uid=uid, runtime_php_bin=php, runtime_user=user)


def cfg():
    return SimpleNamespace(discovery_roots=[], exclude_path_contains=[], supported_mautic_majors=[7], custom_instances=[])


def test_registered_runtime_survives_pre_vhost_and_discovered_rescans(tmp_path, monkeypatch):
    inv = InstanceInventory(str(tmp_path/'state.sqlite'))
    try:
        register(inv, 'one', '/srv/one', '/usr/bin/php8.3', 'one-user')
        register(inv, 'two', '/srv/two', '/usr/bin/php8.4', 'two-user')
        monkeypatch.setattr(module, 'discover_mautic', lambda *args: [])
        assert inv.rescan(cfg()) == 2
        monkeypatch.setattr(module, 'discover_mautic', lambda *args: [MauticInstall(instance_uid='new-guessed-uid',name='one',root='/srv/one',console_path='/srv/one/bin/console')])
        assert inv.rescan(cfg()) == 2
        rows = {x.instance_uid:x for x in inv.list_instances()}
        assert set(rows) == {'one', 'two'}
        assert (rows['one'].runtime_php_bin, rows['one'].runtime_user) == ('/usr/bin/php8.3', 'one-user')
        assert (rows['two'].root, rows['two'].runtime_php_bin) == ('/srv/two', '/usr/bin/php8.4')
    finally:
        inv.conn.close()


def test_identity_conflicts_and_incomplete_registration_preserve_neighbors(tmp_path):
    inv = InstanceInventory(str(tmp_path/'state.sqlite'))
    try:
        register(inv, 'one', '/srv/one', '/usr/bin/php8.3', 'one-user')
        for uid,root,php,user in [('one','/srv/two','/usr/bin/php8.4','two-user'),('other','/srv/one','/usr/bin/php8.4','two-user'),('two','/srv/two','/usr/bin/php8.4',None)]:
            with pytest.raises(ValueError): register(inv, uid, root, php, user)
        rows = inv.list_instances()
        assert len(rows) == 1 and rows[0].instance_uid == 'one'
        assert rows[0].runtime_php_bin == '/usr/bin/php8.3'
    finally:
        inv.conn.close()


def test_owned_receipt_replay_and_rescan_guarded_rollback(tmp_path, monkeypatch):
    inv = InstanceInventory(str(tmp_path/'state.sqlite'))
    values = dict(name='one',root='/srv/one',console_path='/srv/one/bin/console',local_php_path=None,mautic_major=7,db_host=None,db_port=None,db_name=None,db_user=None,db_password=None,db_table_prefix=None,instance_uid='one',runtime_php_bin='/usr/bin/php8.3',runtime_user='one-user')
    try:
        created = inv.add_or_update_manual(registration_receipt=True, registering_operation_id="job-123", **values)
        assert created['created'] and created['registering_operation_id']
        replay = inv.add_or_update_manual(registration_receipt=True, registering_operation_id="job-123", **values)
        assert replay['created'] is False and replay['registering_operation_id'] is None
        register(inv, 'two', '/srv/two', '/usr/bin/php8.4', 'two-user')
        monkeypatch.setattr(module, 'discover_mautic', lambda *args: [])
        inv.rescan(cfg())
        token = created['registering_operation_id']
        assert not inv.remove_owned_registration(instance_uid='one', root='/srv/one', registering_operation_id='wrong')
        assert not inv.remove_owned_registration(instance_uid='two', root='/srv/two', registering_operation_id=token)
        assert not inv.remove_owned_registration(instance_uid='one', root='/srv/two', registering_operation_id=token)
        assert inv.remove_owned_registration(instance_uid='one', root='/srv/one', registering_operation_id=token)
        assert not inv.remove_owned_registration(instance_uid='one', root='/srv/one', registering_operation_id=token)
        assert [row.instance_uid for row in inv.list_instances()] == ['two']
    finally:
        inv.conn.close()


def test_receipt_replay_keeps_existing_database_and_does_not_rebind_runtime(tmp_path):
    inv = InstanceInventory(str(tmp_path/'state.sqlite'))
    values = dict(name='one',root='/srv/one',console_path='/srv/one/bin/console',local_php_path=None,mautic_major=7,db_host='db',db_port=3306,db_name='one',db_user='user',db_password='fixture-only',db_table_prefix='ss_',instance_uid='one',runtime_php_bin='/usr/bin/php8.3',runtime_user='one-user')
    try:
        inv.add_or_update_manual(**values)
        values.update(db_host=None, db_password=None)
        assert inv.add_or_update_manual(registration_receipt=True, registering_operation_id="job-123", **values)['created'] is False
        assert inv.list_instances()[0].db.name == 'one'
        values['runtime_php_bin'] = '/usr/bin/php8.4'
        with pytest.raises(ValueError): inv.add_or_update_manual(registration_receipt=True, registering_operation_id="job-123", **values)
        assert inv.list_instances()[0].runtime_php_bin == '/usr/bin/php8.3'
    finally:
        inv.conn.close()
