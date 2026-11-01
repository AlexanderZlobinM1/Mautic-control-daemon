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


def test_descriptor_entrypoint_mismatch_rejected_before_download(monkeypatch):
    from mcd_agent import custom_scripts as custom
    monkeypatch.setattr(custom, 'fetch_custom_manifest', lambda *a, **kw: ([{'key': 'any', 'entrypoint':'other.py'}], 'test'))
    monkeypatch.setattr(custom, '_fetch_script_to_cache', lambda *a: pytest.fail('descriptor mismatch'))
    code, output = custom.run_custom_script_by_key(None, script_key='any', expected_entrypoint='recipe.py')
    assert code == 1
    assert 'entrypoint' in output


def test_capture_waits_for_long_child_after_output_streams_close(tmp_path):
    # Some recipes finish logging before their long final operation completes.
    # Closing both pipes must not activate the framed runner's five-second wait.
    script=tmp_path/'close-streams.py'
    script.write_text('#!'+sys.executable+'\nimport os,time\nprint('+repr(json.dumps(terminal()))+',flush=True)\nos.close(1)\nos.close(2)\ntime.sleep(5.2)\n')
    script.chmod(0o700)
    code, output=_run_capture_result(script,[])
    assert code==0
    assert json.loads(output)==terminal()['result']


def private_child(tmp_path, *, failure=False, echo=False):
    # Value is supplied only over stdin, never serialized in this executable.
    script = tmp_path / 'private-runner.py'
    script.write_text('#!' + sys.executable + '''
import sys,json,struct
size=struct.unpack('!I',sys.stdin.buffer.read(4))[0]
value=json.loads(sys.stdin.buffer.read(size))['registration_password']
assert sys.stdin.buffer.read()==b''
assert value=="  synthetic:é\\\"\\\\\\tline\\nend\\n\\n"
''' + ('sys.stderr.write(value)\n' if echo else '') +
        ('print("not-json");sys.exit(7)\n' if failure else
         'print(' + repr(json.dumps(terminal())) + ')\n'))
    script.chmod(0o700)
    return script


@pytest.mark.parametrize('failure,echo', [(False, False), (True, False), (False, True)])
def test_private_stdin_exact_special_characters_and_failure_does_not_disclose(tmp_path, failure, echo):
    value = '  synthetic:é"\\\tline\nend\n\n'
    code, output = _run_capture_result(private_child(tmp_path, failure=failure, echo=echo), [],
                                       runtime_input={'registration_password': value})
    assert (code == 0) == (not failure and not echo)
    assert value not in output and value.rstrip('\n') not in output
    if not failure and not echo:
        assert json.loads(output) == terminal()['result']
    elif echo:
        assert output == 'custom private output rejected'
    else:
        assert output == 'custom structured result rejected'


def test_private_execution_exception_is_not_public(tmp_path, monkeypatch):
    from mcd_agent.commercial_self_hosted import ExternalProcessPolicy
    secret = 'SYNTHETIC-EXCEPTION-PRIVATE'
    def fail(*a, **kw):
        raise ValueError(secret)
    monkeypatch.setattr(ExternalProcessPolicy, '_run_bounded', fail)
    code, output = _run_capture_result(tmp_path/'x', [], runtime_input={'registration_password':secret})
    assert code != 0 and secret not in output
    assert output == 'custom structured execution rejected'


def test_private_input_cannot_detach_or_use_direct_logging(monkeypatch):
    from mcd_agent.custom_scripts import run_custom_script_by_key
    code, output = run_custom_script_by_key(None, script_key='x', runtime_input={'registration_password':'synthetic'})
    assert code == 2 and 'synchronous' in output


def test_private_valid_failed_terminal_preserves_existing_outcome(tmp_path):
    frame=terminal(status='failed',exit_code=7,error_code='operation_failed',result={'status':'error','reason':'synthetic_operation_failed'})
    script=tmp_path/'failed.py'
    script.write_text('#!'+sys.executable+'\nimport sys,struct,json\nn=struct.unpack("!I",sys.stdin.buffer.read(4))[0]\njson.loads(sys.stdin.buffer.read(n))\nprint('+repr(json.dumps(frame))+')\nsys.exit(7)\n')
    script.chmod(0o700)
    code,output=_run_capture_result(script,[],runtime_input={'registration_password':'SYNTHETIC-PRIVATE'})
    assert code==7 and json.loads(output)==frame


def test_private_frame_uses_existing_context_limit_before_start(tmp_path,monkeypatch):
    from mcd_agent.commercial_self_hosted import ExternalProcessPolicy
    from mcd_agent.scenario_launcher import MAX_CONTEXT_BYTES
    monkeypatch.setattr(ExternalProcessPolicy,'_run_bounded',lambda *a,**kw:pytest.fail('oversize before process'))
    code,output=_run_capture_result(tmp_path/'x',[],runtime_input={'registration_password':'é'*MAX_CONTEXT_BYTES})
    assert code==2 and output=='custom runtime input invalid'
