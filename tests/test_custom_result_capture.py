import json
import sys

import pytest

from mcd_agent.custom_scripts import _run_capture_result


def terminal(**overrides):
    value = dict(schema='mcd-self-hosted-executable-output-v1', contract_version='1.0',
                 frame_type='result', status='succeeded', exit_code=0, completed=1,
                 total=1, rollback='not_attempted', result={'status': 'noop', 'changed_files': []})
    value.update(overrides)
    return value


def child(tmp_path, frames, *, rc=0, stderr='diagnostic only'):
    script = tmp_path / 'runner.py'
    script.write_text('#!' + sys.executable + '\nimport sys\n' +
                      'sys.stderr.write(' + repr(stderr) + ')\n' +
                      ''.join('print(' + repr(json.dumps(frame)) + ')\n' for frame in frames) +
                      'sys.exit(' + str(rc) + ')\n')
    script.chmod(0o700)
    return script


def test_actual_child_returns_exact_public_result_and_ignores_stderr(tmp_path):
    progress = dict(schema='mcd-self-hosted-executable-output-v1', contract_version='1.0',
                    frame_type='progress', status='running', sequence=1, message_code='apply.start')
    script = child(tmp_path, [progress, terminal()], stderr=json.dumps(terminal(result={'fake': True})))
    rc, output = _run_capture_result(script, [], cwd=tmp_path)
    assert rc == 0
    assert json.loads(output) == terminal()['result']


@pytest.mark.parametrize('frames,rc', [([], 0), ([terminal(), terminal()], 0),
    ([terminal()], 3), ([terminal(result=[])], 0), ([{'status': 'ok'}], 0)])
def test_actual_child_rejects_missing_invalid_duplicate_or_mismatched_terminal(tmp_path, frames, rc):
    code, output = _run_capture_result(child(tmp_path, frames, rc=rc), [])
    assert code != 0
    assert output.startswith('custom structured result rejected:')


def test_missing_public_result_rejected(tmp_path):
    frame = terminal()
    del frame['result']
    code, output = _run_capture_result(child(tmp_path, [frame]), [])
    assert code != 0
    assert 'custom_public_result_missing' in output


def test_failed_terminal_stays_failed(tmp_path):
    frame = terminal(status='failed', exit_code=7, error_code='validation_failed')
    code, output = _run_capture_result(child(tmp_path, [frame], rc=7), [])
    assert code == 7
    assert json.loads(output)['status'] == 'failed'


def test_missing_executable_returns_failure(tmp_path):
    code, output = _run_capture_result(tmp_path / 'missing', [])
    assert code != 0
    assert output.startswith('custom structured execution rejected:')


@pytest.mark.parametrize('options', [{'detach': True}, {'fetch_only': True}])
def test_incompatible_capture_modes_rejected_before_network(options):
    from mcd_agent.custom_scripts import run_custom_script_by_key
    code, output = run_custom_script_by_key(None, script_key='any', capture_result=True, **options)
    assert code == 2
    assert 'synchronous' in output


def test_interactive_capture_rejected_before_download(monkeypatch):
    from mcd_agent import custom_scripts as custom
    monkeypatch.setattr(custom, 'fetch_custom_manifest', lambda *a, **kw: ([{'key': 'any', 'interactive': True}], 'test'))
    monkeypatch.setattr(custom, '_fetch_script_to_cache', lambda *a: pytest.fail('must reject before download'))
    code, output = custom.run_custom_script_by_key(None, script_key='any', capture_result=True)
    assert code == 2
    assert 'interactive' in output
