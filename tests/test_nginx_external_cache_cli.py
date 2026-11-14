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
    body=b'#!/usr/bin/env python3\nimport json,sys\nr=json.loads(sys.argv[1]);code=1 if "error" in r else 0\nprint(json.dumps({"schema":"mcd-self-hosted-executable-output-v1","contract_version":"1.0","frame_type":"result","status":"failed" if code else "succeeded","exit_code":code,"completed":1,"total":1,"rollback":"not_attempted","result":r,**({"error_code":"operation_failed"} if code else {})}))\nsys.exit(code)\n'
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


def select_vhost(root,operation,parameters,result):
    import copy
    manifest=json.loads((root/'manifest.json').read_text())
    response=copy.deepcopy(next(iter(manifest['selected_tasks'].values()))['response'])
    response.update(operation=operation,operation_input=parameters,profile=None,resolved_plan_key='nginx:'+operation)
    handler=response['external_handler']
    handler.update(operation=operation,operation_input=parameters,resolved_plan_key=response['resolved_plan_key'])
    handler['profile_input']={'source':'response.profile','fields':[],'sha256':digest(None)}
    handler['operation_input_input']={'sha256':digest(parameters)}
    handler['command']['argv'][-1]=json.dumps(result)
    custom.cache_selected_task(SimpleNamespace(custom_cache_dir=str(root)),response)


@pytest.mark.parametrize('component,operation', [('migration-vhost','write_nginx_vhost'),('composer-vhost','write_switched_vhost')])
def test_complete_vhost_stage_runs_from_manual_cache_and_preserves_legacy_return(manual_cache,tmp_path,capsys,component,operation):
    from mcd_agent import nginx_external
    root,_=manual_cache
    if component=='migration-vhost':
        parameters={'root':str(tmp_path/'app'),'domains':['qa.example.invalid'],'php_version':'8.5'}
        result={'site':str(tmp_path/'site.conf')}
    else:
        parameters={'source_root':str(tmp_path/'old'),'nginx_root':str(tmp_path/'new'/'docroot'),'php_version':'8.5','site_available':str(tmp_path/'site.conf'),'site_enabled':None}
        result={'active':str(tmp_path/'site.conf'),'backup':str(tmp_path/'backup.conf')}
    select_vhost(root,operation,parameters,result)
    with patch.object(sys,'argv',['mcd-cli','nginx-baseline',component,'apply','--operation-input',json.dumps(parameters),'--json']):
        assert cli.main()==0
    assert json.loads(capsys.readouterr().out)=={'status':'ok',**result}
    if component=='migration-vhost':
        assert nginx_external.write_nginx_vhost(root=Path(parameters['root']),domains=parameters['domains'],php_version=parameters['php_version'])==result['site']
    else:
        assert nginx_external.write_switched_vhost(SimpleNamespace(**{key:(Path(value) if key!='php_version' and value is not None else value) for key,value in parameters.items()}))==result


def test_failed_vhost_terminal_preserves_exact_legacy_error_without_fallback(manual_cache,tmp_path,capsys):
    from mcd_agent import nginx_external
    root,_=manual_cache
    parameters={'root':str(tmp_path/'app'),'domains':['qa.example.invalid'],'php_version':'99.99'}
    error='target PHP-FPM socket is missing: /run/php/php99.99-fpm.sock'
    select_vhost(root,'write_nginx_vhost',parameters,{'error':error})
    with pytest.raises(RuntimeError,match='target PHP-FPM socket is missing') as caught:
        nginx_external.write_nginx_vhost(root=Path(parameters['root']),domains=parameters['domains'],php_version=parameters['php_version'])
    assert str(caught.value)==error
    with patch.object(sys,'argv',['mcd-cli','nginx-baseline','migration-vhost','apply','--operation-input',json.dumps(parameters),'--json']):assert cli.main()==1
    assert json.loads(capsys.readouterr().out)=={'status':'error','reason':error}


def _managed_receipt(tmp_path, operation_id='image-1', domain='qa.example.invalid'):
    site=str(tmp_path/(domain+'.conf'))
    return {
        'schema':'mcc-nginx-managed-vhost-ownership-v1',
        'operation_id':operation_id,
        'domain':domain,
        'site':site,
        'available_path':site,
        'enabled_path':str(tmp_path/'enabled'/(domain+'.conf')),
        'enabled_target':site,
        'available_sha256':'a'*64,
        'created_available':True,
        'created_enabled_symlink':True,
    }


def test_managed_vhost_write_and_receipt_cleanup_use_additive_operations(manual_cache,tmp_path):
    from mcd_agent import nginx_external
    root,_=manual_cache
    operation_id='image-1';domain='qa.example.invalid'
    parameters={'operation_id':operation_id,'root':str(tmp_path/'app'),'domains':[domain],'php_version':'8.5'}
    receipt=_managed_receipt(tmp_path,operation_id,domain)
    write_result={'site':receipt['site'],'ownership_receipt':receipt}
    select_vhost(root,'write_managed_nginx_vhost',parameters,write_result)
    assert nginx_external.write_managed_nginx_vhost(
        operation_id=operation_id,root=Path(parameters['root']),domains=[domain],php_version='8.5'
    )==write_result

    cleanup_parameters={'operation_id':operation_id,'domain':domain,'ownership_receipt':receipt}
    cleanup_result={
        'schema':'mcc-nginx-managed-vhost-cleanup-v1','status':'applied',
        'operation_id':operation_id,'domain':domain,'ownership_receipt_sha256':'b'*64,
        'removed':{'available':True,'enabled':True},'mutation_started':True,
        'rollback_available':False,
    }
    select_vhost(root,'cleanup_managed_vhost',cleanup_parameters,cleanup_result)
    assert nginx_external.cleanup_managed_vhost(
        operation_id=operation_id,domain=domain,ownership_receipt=receipt
    )==cleanup_result


def test_managed_cleanup_rejects_foreign_receipt_before_recipe(manual_cache,tmp_path,monkeypatch):
    from mcd_agent import nginx_external
    from mcd_agent.commercial_self_hosted import ExternalProcessPolicy
    receipt=_managed_receipt(tmp_path,operation_id='foreign')
    monkeypatch.setattr(ExternalProcessPolicy,'_run_bounded',lambda *a,**kw:pytest.fail('foreign receipt must not start recipe'))
    with pytest.raises(custom.CustomSelectedTaskError,match='nginx_operation_input_invalid'):
        nginx_external.cleanup_managed_vhost(
            operation_id='image-1',domain='qa.example.invalid',ownership_receipt=receipt
        )


@pytest.mark.parametrize('problem',['input-changed','extra-flag','wrong-type','status','no-reload'])
def test_vhost_unselected_inputs_or_invented_modes_never_start_recipe(manual_cache,tmp_path,monkeypatch,capsys,problem):
    from mcd_agent.commercial_self_hosted import ExternalProcessPolicy
    root,_=manual_cache
    parameters={'root':str(tmp_path/'app'),'domains':['qa.example.invalid'],'php_version':'8.5'}
    select_vhost(root,'write_nginx_vhost',parameters,{'site':str(tmp_path/'site.conf')})
    monkeypatch.setattr(ExternalProcessPolicy,'_run_bounded',lambda *a,**kw:pytest.fail('invalid input must not start recipe'))
    operation='status' if problem=='status' else 'apply';extra=['--no-reload'] if problem=='no-reload' else []
    if problem=='input-changed':parameters['root']=str(tmp_path/'different-app')
    if problem=='extra-flag':parameters['reload_service']=False
    if problem=='wrong-type':parameters['domains']=[True]
    with patch.object(sys,'argv',['mcd-cli','nginx-baseline','migration-vhost',operation,'--operation-input',json.dumps(parameters),'--json',*extra]):assert cli.main()==1
    result=json.loads(capsys.readouterr().out)
    assert result['reason']==('custom_selected_task_missing' if problem=='input-changed' else 'nginx_operation_input_invalid')
    assert not (tmp_path/'site.conf').exists()


@pytest.mark.parametrize('corrupt',[False,True])
def test_available_transport_repairs_only_selected_pinned_artifact_then_stays_offline(manual_cache,tmp_path,monkeypatch,corrupt):
    from mcd_agent import nginx_external,service_profiles
    root,artifact=manual_cache
    parameters={'root':str(tmp_path/'app'),'domains':['qa.example.invalid'],'php_version':'8.5'}
    expected={'site':str(tmp_path/'site.conf')}
    select_vhost(root,'write_nginx_vhost',parameters,expected)
    data=artifact.read_bytes()
    if corrupt:artifact.write_bytes(b'corrupt required archive')
    else:artifact.unlink()
    cfg=custom.standalone_custom_cache_config(root);cfg.mcc_url='https://explicit-artifact-transport.invalid'
    calls=[]
    def download(request,**kwargs):
        calls.append(request.full_url)
        assert request.full_url=='https://explicit-artifact-transport.invalid/wire-fixture.tar.gz'
        return BytesIO(data)
    monkeypatch.setattr(custom.urllib.request,'urlopen',download)
    monkeypatch.setattr(service_profiles,'fetch_service_profile',lambda *a,**kw:pytest.fail('repair must preserve local selection, not query latest'))
    for _ in range(2):
        assert nginx_external.write_nginx_vhost(root=Path(parameters['root']),domains=parameters['domains'],php_version=parameters['php_version'],cfg=cfg)==expected['site']
    assert len(calls)==1 and artifact.read_bytes()==data
