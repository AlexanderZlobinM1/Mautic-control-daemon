import json
import sys
from unittest.mock import patch

import pytest

from mcd_agent import cli, nginx_baseline as baseline


@pytest.fixture
def stub(tmp_path, monkeypatch):
    conf = tmp_path / 'conf.d'
    conf.mkdir()
    path = conf / '00-mcd-default-deny.conf'
    path.write_text(baseline.DEFAULT_DENY_MARKER + '\n# old managed stub\n')
    monkeypatch.setattr(baseline, 'DEFAULT_DENY_FILE', path)
    monkeypatch.setattr(baseline, 'BACKUP_ROOT', tmp_path / 'backups')
    monkeypatch.setattr(baseline, '_active_default_server_ports', lambda: set())
    monkeypatch.setattr(baseline.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(baseline.shutil, 'which', lambda command: '/fixture/' + command)
    return path


def test_status_is_read_only(stub):
    old = stub.read_bytes()
    with patch.object(baseline, '_nginx_test') as test, patch.object(baseline, '_reload_nginx') as reload:
        receipt = baseline.default_stub_operation()
    assert receipt['status'] == 'drift'
    assert stub.read_bytes() == old
    assert not baseline.BACKUP_ROOT.exists()
    test.assert_not_called()
    reload.assert_not_called()


def test_apply_changes_only_stub_and_replay_does_not_reload(stub):
    neighbor = stub.parent / 'keep.conf'
    neighbor.write_bytes(b'neighbor vhost must stay exactly as it was\n')
    original, mtime = neighbor.read_bytes(), neighbor.stat().st_mtime_ns
    events = []
    def validate():
        events.append('test')
        assert 'ssl_reject_handshake on;' in stub.read_text()
        assert 'ssl_certificate' not in stub.read_text()
        return True, 'nginx_test:ok'
    def reload():
        events.append('reload')
        return True, 'nginx_reload:ok'
    with patch.object(baseline, '_nginx_test', side_effect=validate), patch.object(baseline, '_reload_nginx', side_effect=reload):
        receipt = baseline.default_stub_operation(apply=True)
        replay = baseline.default_stub_operation(apply=True)
    assert receipt['status'] == 'ok' and receipt['changed'] and receipt['validated'] and receipt['reloaded']
    assert replay['status'] == 'ok' and not replay['changed'] and not replay['reloaded']
    assert events == ['test', 'reload', 'test']
    assert neighbor.read_bytes() == original and neighbor.stat().st_mtime_ns == mtime
    assert set(stub.parent.iterdir()) == {stub, neighbor}


def test_validation_failure_restores_only_stub(stub):
    old = stub.read_bytes()
    with patch.object(baseline, '_nginx_test', return_value=(False, 'fixture error')), patch.object(baseline, '_reload_nginx') as reload:
        receipt = baseline.default_stub_operation(apply=True)
    assert receipt['status'] == 'error' and receipt['reason'] == 'nginx_validation_failed'
    assert receipt['rollback'] is True and stub.read_bytes() == old
    reload.assert_not_called()


def test_skipped_validation_is_never_claimed_as_pass(stub):
    old = stub.read_bytes()
    with patch.object(baseline, '_nginx_test', return_value=(True, 'nginx_test:skipped_no_binary')), patch.object(baseline, '_reload_nginx') as reload:
        receipt = baseline.default_stub_operation(apply=True)
    assert receipt['status'] == 'error' and receipt['validated'] is False
    assert stub.read_bytes() == old
    reload.assert_not_called()


def test_reload_failure_restores_file_and_reloads_valid_original(stub):
    old = stub.read_bytes()
    with patch.object(baseline, '_nginx_test', return_value=(True, 'nginx_test:ok')), patch.object(baseline, '_reload_nginx', side_effect=[(False, 'failed'), (True, 'nginx_reload:ok')]):
        receipt = baseline.default_stub_operation(apply=True)
    assert receipt['status'] == 'error' and receipt['reason'] == 'nginx_reload_failed'
    assert receipt['rollback'] is True and receipt['rollback_reload'] is True
    assert stub.read_bytes() == old


@pytest.mark.parametrize('kind', ['unmanaged', 'symlink'])
def test_unmanaged_or_symlink_is_never_overwritten(stub, kind):
    stub.write_text('operator managed file')
    target = stub.parent / 'keep.conf'
    if kind == 'symlink':
        stub.rename(target)
        stub.symlink_to(target)
    with patch.object(baseline, '_nginx_test') as test:
        receipt = baseline.default_stub_operation(apply=True)
    assert receipt['status'] == 'blocked'
    assert stub.read_text() == 'operator managed file'
    assert not baseline.BACKUP_ROOT.exists()
    test.assert_not_called()


def test_concurrent_edit_is_not_overwritten_by_rollback(stub):
    def validate():
        stub.write_text('operator concurrent change')
        return False, 'fixture'
    with patch.object(baseline, '_nginx_test', side_effect=validate):
        receipt = baseline.default_stub_operation(apply=True)
    assert receipt['reason'] == 'default_stub_changed_during_apply'
    assert receipt['rollback'] is False
    assert stub.read_text() == 'operator concurrent change'


def test_failed_new_stub_is_removed(stub):
    stub.unlink()
    with patch.object(baseline, '_nginx_test', return_value=(False, 'fixture')):
        receipt = baseline.default_stub_operation(apply=True)
    assert receipt['rollback'] is True
    assert not stub.exists()


def test_cli_is_standalone_and_routes_only_default_stub(capsys):
    args = ['mcd-cli', 'nginx-baseline', 'default-stub', 'apply', '--json']
    with patch.object(sys, 'argv', args), patch.object(cli, 'load_config', side_effect=AssertionError('must not load instance/MCC config')), patch.object(cli, 'default_stub_operation', return_value={'status': 'ok'}) as operation:
        assert cli.main() == 0
    operation.assert_called_once_with(apply=True)
    assert json.loads(capsys.readouterr().out)['status'] == 'ok'
