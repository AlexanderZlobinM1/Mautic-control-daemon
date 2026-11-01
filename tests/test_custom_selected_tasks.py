import copy
import hashlib
import json
from io import BytesIO
from types import SimpleNamespace

import pytest

from mcd_agent import custom_scripts as custom


@pytest.fixture
def cached(tmp_path):
    cfg=SimpleNamespace(custom_cache_dir=str(tmp_path/'cache'),custom_repo_base_url='',plugins_repo_base_url='',mcc_url='',mcc_token=None,custom_manifest_path='manifest.json',custom_run_mode_default='direct')
    root=tmp_path/'cache';root.mkdir()
    body=b'#!/bin/sh\nprintf verified-cache\n';digest=hashlib.sha256(body).hexdigest()
    row={'key':'selected-fixture','file':'selected.sh','sha256':digest,'run_mode':'direct','interactive':False}
    (root/'manifest.json').write_text(json.dumps({'items':[row]}))
    file=root/'downloads'/row['key']/(row['key']+'-'+digest+'.sh');file.parent.mkdir(parents=True);file.write_bytes(body)
    response={'status':'ok','component':'nginx','operation':'default_stub_operation','operation_input':{'apply':False},'profile':{},'external_handler':{'active':True,'state':'active','artifact':{'key':row['key'],'sha256':digest,'entrypoint':None}}}
    response['resolved_plan_key']='nginx:fixture'
    response['external_handler'].update(schema='mcc-service-profile-external-handler-v1',transport='mcd-custom-v1',component='nginx',operation=response['operation'],operation_input=copy.deepcopy(response['operation_input']),resolved_plan_key=response['resolved_plan_key'])
    context={'ipv6_listen_forbidden':False}
    response['runtime_context']=context
    response['external_handler'].update(runtime_context=context,profile_input={'source':'response.profile','sha256':custom._public_json_digest({}),'fields':[]},runtime_context_input={'sha256':custom._public_json_digest(context)},operation_input_input={'sha256':custom._public_json_digest(response['operation_input'])})
    return cfg,root,row,file,response


def test_manual_selected_task_and_verified_cache_need_no_config_registration_url_or_get(cached,monkeypatch):
    cfg,root,row,file,response=cached
    record={'schema':'mcd-custom-selected-task-v1','component':'nginx','operation':response['operation'],'operation_input':response['operation_input'],'response':response}
    key=custom.selected_task_key('nginx',response['operation'],response['operation_input'],profile_sha256=response['external_handler']['profile_input']['sha256'],runtime_context_sha256=response['external_handler']['runtime_context_input']['sha256'])
    (root/'manifest.json').write_text(json.dumps({'items':[row],'selected_tasks':{key:record}}))
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('complete selected cache must not request HTTP'))
    selected=custom.cached_selected_task(cfg,component='nginx',operation='default_stub_operation',operation_input={'apply':False})
    assert selected==response
    assert custom.run_custom_script_by_key(cfg,script_key=row['key'],expected_sha256=row['sha256'],detach=False,live_output=False,offline_only=True)==(0,'verified-cache')


def test_status_apply_variants_survive_remote_catalog_refresh_without_remote_selection_authority(cached,monkeypatch):
    cfg,root,row,file,response=cached
    for value in [False,True]:
        current=copy.deepcopy(response);current['operation_input']={'apply':value};current['external_handler']['operation_input']=copy.deepcopy(current['operation_input']);current['external_handler']['operation_input_input']['sha256']=custom._public_json_digest(current['operation_input']);custom.cache_selected_task(cfg,current)
    cfg.custom_repo_base_url='https://fixture.invalid'
    # A global response cannot replace local operator/MCC-selected coordinates.
    remote={'items':[row],'selected_tasks':{'remote-malicious':{'runtime_input':'must-not-persist'}}}
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:BytesIO(json.dumps(remote).encode()))
    custom.fetch_custom_manifest(cfg)
    saved=json.loads((root/'manifest.json').read_text())
    assert len(saved['selected_tasks'])==2 and 'remote-malicious' not in saved['selected_tasks']
    for value in [False,True]:assert custom.cached_selected_task(cfg,component='nginx',operation='default_stub_operation',operation_input={'apply':value})['operation_input']=={'apply':value}
    assert not list(root.glob('.manifest-*'))


@pytest.mark.parametrize('field',['runtime_input','credentials','password','api_token'])
def test_private_data_is_rejected_without_cache_mutation(cached,field):
    cfg,root,row,file,response=cached
    before=(root/'manifest.json').read_bytes();response[field]={'private':'synthetic-value'}
    with pytest.raises(custom.CustomSelectedTaskError,match='invalid'):custom.cache_selected_task(cfg,response)
    assert (root/'manifest.json').read_bytes()==before and not list(root.glob('.manifest-*'))


@pytest.mark.parametrize("remote_missing", [False, True])
def test_remote_catalog_removal_preserves_selected_pins_and_offline_execution(cached,monkeypatch,remote_missing):
    cfg,root,row,file,response=cached;custom.cache_selected_task(cfg,response)
    cfg.custom_repo_base_url='https://fixture.invalid'
    calls=[]
    def refresh(*args,**kwargs):
        calls.append('explicit-refresh')
        if remote_missing:
            raise custom.urllib.error.HTTPError('https://fixture.invalid',404,'missing',{},None)
        return BytesIO(b'{"items":[]}')
    monkeypatch.setattr(custom.urllib.request,'urlopen',refresh)
    rows,source=custom.fetch_custom_manifest(cfg)
    assert source==('remote-empty' if remote_missing else 'remote') and any(item['key']==row['key'] and item['sha256']==row['sha256'] for item in rows)
    assert custom.cached_selected_task(cfg,component='nginx',operation='default_stub_operation',operation_input={'apply':False})==response
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('remote latest must not gate cached execution'))
    assert custom.run_custom_script_by_key(cfg,script_key=row['key'],expected_sha256=row['sha256'],detach=False,live_output=False,offline_only=True)==(0,'verified-cache')
    assert calls==['explicit-refresh'] and file.exists()


def test_missing_selection_is_read_only_without_even_creating_cache(tmp_path):
    cfg=SimpleNamespace(custom_cache_dir=str(tmp_path/'absent'))
    with pytest.raises(custom.CustomSelectedTaskError,match='missing'):custom.cached_selected_task(cfg,component='nginx',operation='default_stub_operation',operation_input={'apply':True})
    assert not (tmp_path/'absent').exists()


@pytest.mark.parametrize('corrupt',[False,True])
def test_missing_or_corrupt_selected_artifact_fails_before_process_or_network(cached,monkeypatch,corrupt):
    cfg,root,row,file,response=cached
    if corrupt:file.write_bytes(b'corrupt')
    else:file.unlink()
    before={str(p) for p in root.rglob('*')}
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('offline task must not contact controller'))
    monkeypatch.setattr(custom,'_run_direct',lambda *a,**kw:pytest.fail('missing artifact must not execute'))
    code,out=custom.run_custom_script_by_key(cfg,script_key=row['key'],expected_sha256=row['sha256'],detach=False,offline_only=True)
    assert code!=0 and 'custom_selected_task_artifact_missing_or_corrupt' in out
    assert {str(p) for p in root.rglob('*')}==before


def test_observer_none_and_configured_profile_selections_coexist_offline(cached):
    cfg,root,row,file,response=cached
    response['operation']='cloudflare_real_ip_state';response['operation_input']={}
    response['external_handler'].update(operation=response['operation'],operation_input={})
    response['external_handler']['operation_input_input']['sha256']=custom._public_json_digest({})
    for profile in [None,{'setting':True}]:
        current=copy.deepcopy(response);current['profile']=profile
        current['external_handler']['profile_input']={'source':'response.profile','sha256':custom._public_json_digest(profile),'fields':['setting']}
        custom.cache_selected_task(cfg,current)
    empty=custom.cached_selected_task(cfg,component='nginx',operation='cloudflare_real_ip_state',operation_input={},profile=None)
    configured=custom.cached_selected_task(cfg,component='nginx',operation='cloudflare_real_ip_state',operation_input={},profile={'setting':True,'unrelated':42})
    assert empty['profile'] is None and configured['profile']=={'setting':True}
    assert len(json.loads((root/'manifest.json').read_text())['selected_tasks'])==2


def test_full_projection_field_list_detects_newly_present_caller_field(cached):
    cfg,root,row,file,response=cached
    response['external_handler']['profile_input']['fields']=['setting']
    custom.cache_selected_task(cfg,response)
    with pytest.raises(custom.CustomSelectedTaskError,match='input_mismatch'):
        custom.cached_selected_task(cfg,component='nginx',operation='default_stub_operation',operation_input={'apply':False},profile={'setting':True})


def test_changed_current_fact_is_typed_without_fetch_or_any_write(cached,monkeypatch):
    cfg,root,row,file,response=cached;custom.cache_selected_task(cfg,response)
    before=(root/'manifest.json').read_bytes()
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('fact mismatch must not guess/fetch another selection'))
    with pytest.raises(custom.CustomSelectedTaskError,match='input_mismatch'):
        custom.cached_selected_task(cfg,component='nginx',operation='default_stub_operation',operation_input={'apply':False},runtime_context={'ipv6_listen_forbidden':True})
    assert (root/'manifest.json').read_bytes()==before


def test_scheduled_cleanup_keeps_old_selected_task_executable_offline(cached,monkeypatch):
    import os
    cfg,root,row,file,response=cached
    custom.cache_selected_task(cfg,response)
    cfg.custom_logs_keep_days=cfg.custom_downloads_keep_days=1
    cfg.custom_logs_max_files=cfg.custom_downloads_max_entries=1
    unused=root/'downloads'/'unused';unused.mkdir();(unused/'old.sh').write_bytes(b'unselected')
    os.utime(file.parent,(1,1));os.utime(unused,(1,1))
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('selected cache must stay offline'))
    result=custom.cleanup_custom_cache(cfg,known_keys={'unrelated-remote-row'})
    assert result['downloads_removed']==1 and not unused.exists() and file.exists()
    assert custom.cached_selected_task(cfg,component='nginx',operation='default_stub_operation',operation_input={'apply':False})==response
    assert custom.run_custom_script_by_key(cfg,script_key=row['key'],expected_sha256=row['sha256'],detach=False,live_output=False,offline_only=True)==(0,'verified-cache')
