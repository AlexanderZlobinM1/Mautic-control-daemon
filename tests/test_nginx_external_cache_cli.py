import hashlib
import json
import sys
import tarfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mcd_agent import cli, custom_scripts as custom, nginx_baseline


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


@pytest.fixture
def manual_cache(tmp_path,monkeypatch):
    root=tmp_path/'cache';root.mkdir()
    body=b'#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps({"schema":"mcd-self-hosted-executable-output-v1","contract_version":"1.0","frame_type":"result","status":"succeeded","exit_code":0,"completed":1,"total":1,"rollback":"not_attempted","result":json.loads(sys.argv[1])}))\n'
    stream=BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as archive:
        member=tarfile.TarInfo('entry.py');member.size=len(body);archive.addfile(member,BytesIO(body))
    data=stream.getvalue();sha=hashlib.sha256(data).hexdigest();row={'key':'wire-fixture','file':'wire-fixture.tar.gz','sha256':sha,'entrypoint':'entry.py','run_mode':'direct','interactive':False}
    (root/'manifest.json').write_text(json.dumps({'items':[row]}))
    artifact=root/'downloads'/row['key']/(row['key']+'-'+sha+'.tar.gz');artifact.parent.mkdir(parents=True);artifact.write_bytes(data)
    cfg=SimpleNamespace(custom_cache_dir=str(root))
    for apply,result in [(False,{'status':'drift','wire_selection':'inspect'}),(True,{'status':'ok','wire_selection':'apply'})]:
        flags={'apply':apply};context={'ipv6_listen_forbidden':False};plan='explicit-public-fixture'
        response={'status':'ok','component':'nginx','operation':'default_stub_operation','operation_input':flags,'resolved_plan_key':plan,'profile':None,'runtime_context':context}
        response['external_handler']={'schema':'mcc-service-profile-external-handler-v1','transport':'mcd-custom-v1','state':'active','active':True,'component':'nginx','operation':response['operation'],'operation_input':flags,'resolved_plan_key':plan,'runtime_context':context,'profile_input':{'source':'response.profile','fields':[],'sha256':digest(None)},'runtime_context_input':{'sha256':digest(context)},'operation_input_input':{'sha256':digest(flags)},'minimum_runtime':{'required_capabilities':['custom-gzip-entrypoint','raw-jsonline-terminal-result']},'artifact':{**row,'media_type':'application/gzip'},'command':{'argv':['custom','--no-detach','--expected-sha256',sha,row['key'],'--',json.dumps(result)]}}
        custom.cache_selected_task(cfg,response)
    monkeypatch.setenv('MCD_CUSTOM_CACHE_DIR',str(root));monkeypatch.setenv('MCD_CONFIG',str(tmp_path/'absent-config.toml'))
    monkeypatch.setattr(nginx_baseline,'_ipv6_listen_forbidden',lambda:False)
    monkeypatch.setattr(nginx_baseline,'default_stub_operation',lambda **kw:pytest.fail('builtin writer must never be used'))
    monkeypatch.setattr(cli,'load_config',lambda *a,**kw:pytest.fail('nginx CLI must not require registration/config load'))
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('complete offline task has zero GET/HEAD'))
    return root,artifact


@pytest.mark.parametrize('operation,expected',[('status','inspect'),('apply','apply')])
def test_actual_cli_uses_exact_selected_variant_and_existing_runner_without_config(manual_cache,capsys,operation,expected):
    with patch.object(sys,'argv',['mcd-cli','nginx-baseline','default-stub',operation,'--json']):
        assert cli.main()==0
    assert json.loads(capsys.readouterr().out)['wire_selection']==expected


@pytest.mark.parametrize('problem',['selection','artifact','fact'])
def test_actual_cli_missing_or_changed_required_input_is_typed_before_any_writer(manual_cache,monkeypatch,capsys,problem):
    root,artifact=manual_cache
    from mcd_agent.commercial_self_hosted import ExternalProcessPolicy
    monkeypatch.setattr(ExternalProcessPolicy,'_run_bounded',lambda *a,**kw:pytest.fail('invalid closure must not start process'))
    if problem=='selection':(root/'manifest.json').unlink()
    elif problem=='artifact':artifact.unlink()
    else:monkeypatch.setattr(nginx_baseline,'_ipv6_listen_forbidden',lambda:True)
    before={str(p) for p in root.rglob('*')}
    with patch.object(sys,'argv',['mcd-cli','nginx-baseline','default-stub','apply','--json']):
        assert cli.main()==1
    result=json.loads(capsys.readouterr().out)
    expected={'selection':'custom_selected_task_missing','artifact':'custom_selected_task_artifact_missing_or_corrupt','fact':'custom_selected_task_input_mismatch'}[problem]
    assert result['reason']==expected
    assert {str(p) for p in root.rglob('*')}==before
