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
    return cfg,root,row,file,response


def test_manual_selected_task_and_verified_cache_need_no_config_registration_url_or_get(cached,monkeypatch):
    cfg,root,row,file,response=cached
    record={'schema':'mcd-custom-selected-task-v1','component':'nginx','operation':response['operation'],'operation_input':response['operation_input'],'response':response}
    key=custom.selected_task_key('nginx',response['operation'],response['operation_input'])
    (root/'manifest.json').write_text(json.dumps({'items':[row],'selected_tasks':{key:record}}))
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('complete selected cache must not request HTTP'))
    selected=custom.cached_selected_task(cfg,component='nginx',operation='default_stub_operation',operation_input={'apply':False})
    assert selected==response
    assert custom.run_custom_script_by_key(cfg,script_key=row['key'],expected_sha256=row['sha256'],detach=False,live_output=False,offline_only=True)==(0,'verified-cache')


def test_status_apply_variants_survive_remote_catalog_refresh_without_remote_selection_authority(cached,monkeypatch):
    cfg,root,row,file,response=cached
    for value in [False,True]:
        current=copy.deepcopy(response);current['operation_input']={'apply':value};custom.cache_selected_task(cfg,current)
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


def test_catalog_revision_removing_required_item_drops_only_unresolvable_selection(cached,monkeypatch):
    cfg,root,row,file,response=cached;custom.cache_selected_task(cfg,response)
    cfg.custom_repo_base_url='https://fixture.invalid'
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:BytesIO(b'{"items":[]}'))
    custom.fetch_custom_manifest(cfg)
    with pytest.raises(custom.CustomSelectedTaskError,match='missing'):custom.cached_selected_task(cfg,component='nginx',operation='default_stub_operation',operation_input={'apply':False})
    assert file.exists()  # Selection invalidation does not delete another actor's bytes.


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
