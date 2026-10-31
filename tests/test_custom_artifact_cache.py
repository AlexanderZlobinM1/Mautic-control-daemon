from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import pytest
from mcd_agent import custom_scripts as custom


@pytest.fixture
def setup_cache(tmp_path):
    cfg=SimpleNamespace(custom_cache_dir=str(tmp_path),custom_repo_base_url='https://controller.example.org',plugins_repo_base_url='',mcc_url='',custom_manifest_path='custom/manifest.json',mcc_token='existing-key',custom_run_mode_default='direct')
    data=b'#!/bin/sh\nprintf cached\n';sha=hashlib.sha256(data).hexdigest()
    row={'key':'task-r1','file':'scripts/task-r1.sh','sha256':sha,'run_mode':'direct','interactive':False}
    (tmp_path/'manifest.json').write_text(json.dumps({'items':[row]}))
    target=tmp_path/'downloads'/row['key']/f"{row['key']}-{sha}.sh"
    target.parent.mkdir(parents=True);target.write_bytes(data);target.chmod(0o755)
    return cfg,row,data,target


def test_manually_populated_verified_task_cache_executes_with_zero_network(setup_cache,monkeypatch):
    cfg,row,data,target=setup_cache
    target.chmod(0o644)
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('warm task cache must not fetch artifacts or metadata'))
    rc,out=custom.run_custom_script_by_key(cfg,script_key=row['key'],expected_sha256=row['sha256'],detach=False,live_output=False)
    assert (rc,out)==(0,'cached')


def test_corrupt_cache_is_refetched_online_and_only_verified_bytes_execute(setup_cache,monkeypatch):
    cfg,row,data,target=setup_cache;target.write_bytes(b'corrupt')
    calls=[]
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:calls.append(a) or BytesIO(data))
    assert custom.run_custom_script_by_key(cfg,script_key=row['key'],expected_sha256=row['sha256'],detach=False,live_output=False)==(0,'cached')
    assert len(calls)==1 and target.read_bytes()==data


@pytest.mark.parametrize('missing',[False,True])
def test_offline_corrupt_or_missing_required_cache_never_starts(setup_cache,monkeypatch,missing):
    cfg,row,data,target=setup_cache
    if missing:target.unlink()
    else:target.write_bytes(b'wrong bytes')
    def unavailable(*args,**kwargs):raise OSError('offline')
    monkeypatch.setattr(custom.urllib.request,'urlopen',unavailable)
    monkeypatch.setattr(custom,'_run_direct',lambda *a,**kw:pytest.fail('invalid closure must not execute'))
    rc,message=custom.run_custom_script_by_key(cfg,script_key=row['key'],expected_sha256=row['sha256'],detach=False)
    assert rc != 0 and 'offline' in message


def test_fetch_only_validates_manual_cache_without_execution(setup_cache,monkeypatch):
    cfg,row,data,target=setup_cache
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('already cached'))
    monkeypatch.setattr(custom,'_run_direct',lambda *a,**kw:pytest.fail('prefetch must not execute'))
    rc,message=custom.run_custom_script_by_key(cfg,script_key=row['key'],expected_sha256=row['sha256'],fetch_only=True)
    assert rc==0 and json.loads(message)['status']=='cached'


def test_cache_does_not_guess_another_task_revision(setup_cache,monkeypatch):
    cfg,row,data,target=setup_cache
    monkeypatch.setattr(custom,'fetch_custom_manifest',lambda *a,**kw:([row],'cache'))
    monkeypatch.setattr(custom,'_run_direct',lambda *a,**kw:pytest.fail('wrong task revision must not execute'))
    rc,_=custom.run_custom_script_by_key(cfg,script_key=row['key'],expected_sha256='0'*64,detach=False)
    assert rc != 0


def test_standalone_verified_cache_needs_no_repository_or_mcc_url(setup_cache,monkeypatch):
    cfg,row,data,target=setup_cache
    cfg.custom_repo_base_url='';cfg.plugins_repo_base_url='';cfg.mcc_url=''
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('offline standalone task must not request a URL'))
    assert custom.run_custom_script_by_key(cfg,script_key=row['key'],expected_sha256=row['sha256'],detach=False,live_output=False)==(0,'cached')


def test_standalone_cached_catalog_is_available_without_repository_url(setup_cache,monkeypatch):
    cfg,row,data,target=setup_cache
    cfg.custom_repo_base_url='';cfg.plugins_repo_base_url='';cfg.mcc_url=''
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('no repository is configured'))
    rows,source=custom.fetch_custom_manifest(cfg)
    assert source=='cache' and rows[0]['key']==row['key']
