import json
import sys
from types import SimpleNamespace

import pytest

from mcd_agent import cli, executor


def test_exec_parser_preserves_literal_repeated_console_arguments():
    args = cli._build_parser().parse_args([
        'exec', '--root', '/instance', '--command', 'cache:warmup',
        '--php-bin', '/usr/bin/php8.4', '--run-as-user', 'www-data',
        '--timeout', '600', '--console-arg=--env=prod',
        '--console-arg=--no-debug', '--console-arg=--no-interaction',
    ])
    assert args.console_arg == ['--env=prod', '--no-debug', '--no-interaction']
    assert args.php_bin == '/usr/bin/php8.4'
    assert args.run_as_user == 'www-data'


@pytest.mark.parametrize('command,actual', [('version', '--version'), ('doctrine:migrations:migrate', 'doctrine:migrations:migrate'), ('cache:warmup', 'cache:warmup')])
def test_existing_executor_runs_literal_arguments_and_preserves_exit(tmp_path, monkeypatch, command, actual):
    console = tmp_path/'bin'/'console'
    console.parent.mkdir()
    console.write_text('import json,sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(7)\n')
    monkeypatch.setattr(executor, 'descriptor_for_root', lambda root: None)
    literal = '--value={literal};$(echo forbidden)'
    rc, output = executor.execute_mautic_command(
        php_bin=sys.executable, root=str(tmp_path), command=command,
        instance_id=None, timeout_sec=5, run_as_user=None,
        console_args=['--env=prod', '--no-debug', literal],
    )
    assert rc == 7
    assert json.loads(output) == [actual, '--no-interaction', '--env=prod', '--no-debug', literal]


def test_scheduler_adapter_forwards_console_flags_without_new_workflow(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, 'cluster_route_for_command', lambda command: None)
    monkeypatch.setattr(cli, 'cluster_route_targets', lambda *args: [])
    monkeypatch.setattr(cli, 'execute_mautic_command', lambda **kw: calls.append(kw) or (0, 'ok'))
    result = cli._run_manual_command_with_scheduler(
        cfg=SimpleNamespace(profile_name='passive'), root='/instance',
        command='cache:warmup', instance_id=None, php_bin='/usr/bin/php8.4',
        timeout_sec=600, run_as_user='www-data', console_args=['--env=prod'],
    )
    assert result == (0, 'ok')
    assert calls[0]['console_args'] == ['--env=prod']
    assert calls[0]['run_as_user'] == 'www-data'


@pytest.mark.parametrize('uid,root,php,user', [('one', '/srv/one', '/usr/bin/php8.3', 'one-user'), ('two', '/srv/two', '/usr/bin/php8.4', 'two-user')])
def test_exact_identity_resolves_its_own_runtime(monkeypatch, uid, root, php, user):
    rows = [SimpleNamespace(instance_uid='one', root='/srv/one', runtime_php_bin='/usr/bin/php8.3', runtime_user='one-user'), SimpleNamespace(instance_uid='two', root='/srv/two', runtime_php_bin='/usr/bin/php8.4', runtime_user='two-user')]
    monkeypatch.setattr(cli, 'InstanceInventory', lambda path: SimpleNamespace(list_instances=lambda: rows, conn=SimpleNamespace(close=lambda: None)))
    assert cli._resolve_registered_command_runtime(SimpleNamespace(state_db_path='fixture'), uid) == (root, php, user)


@pytest.mark.parametrize('rows', [[], [SimpleNamespace(instance_uid='one'), SimpleNamespace(instance_uid='one')], [SimpleNamespace(instance_uid='one', root='/srv/one', runtime_php_bin=None, runtime_user='user')]])
def test_unknown_ambiguous_or_missing_runtime_fails_without_guess(monkeypatch, rows):
    monkeypatch.setattr(cli, 'InstanceInventory', lambda path: SimpleNamespace(list_instances=lambda: rows, conn=SimpleNamespace(close=lambda: None)))
    with pytest.raises(ValueError):
        cli._resolve_registered_command_runtime(SimpleNamespace(state_db_path='fixture'), 'one')


def test_exec_identity_selector_does_not_require_paths():
    args = cli._build_parser().parse_args(['exec', '--instance-uid', 'one', '--command', 'cache:warmup', '--console-arg=--env=prod'])
    assert args.root is None and args.instance_uid == 'one'
