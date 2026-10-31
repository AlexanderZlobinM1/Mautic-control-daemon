import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcd_agent import custom_scripts as custom


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(custom_cache_dir=str(tmp_path), custom_repo_base_url='https://controller.example.org', plugins_repo_base_url='', mcc_url='', custom_manifest_path='custom/manifest.json', mcc_token='existing-key', custom_run_mode_default='direct')


def row(data):
    return {'key':'scenario-revision-1','name':'Scenario','file':'scripts/revision-1.py','sha256':hashlib.sha256(data).hexdigest(),'run_mode':'direct','interactive':False}


def test_job_pin_mismatch_stops_before_download_or_execution(cfg, monkeypatch):
    monkeypatch.setattr(custom,'fetch_custom_manifest',lambda *a,**kw: ([row(b'approved')],'network'))
    monkeypatch.setattr(custom,'_fetch_script_to_cache',lambda *a: pytest.fail('must not download'))
    rc, message=custom.run_custom_script_by_key(cfg,script_key='scenario-revision-1',expected_sha256='0'*64,detach=False)
    assert rc != 0 and 'requested pin' in message


def test_invalid_job_pin_stops_before_manifest_fetch(cfg, monkeypatch):
    monkeypatch.setattr(custom,'fetch_custom_manifest',lambda *a,**kw: pytest.fail('must not fetch'))
    assert custom.run_custom_script_by_key(cfg,script_key='scenario',expected_sha256='invalid')[0] != 0


def test_verified_download_has_content_addressed_executable_path(cfg, monkeypatch):
    data=b'#!/bin/sh\nexit 0\n'
    item=row(data)
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw: BytesIO(data))
    path=custom._fetch_script_to_cache(cfg,item)
    assert path.read_bytes()==data
    assert item['sha256'] in path.name
    assert path.stat().st_mode & 0o777 == 0o755
    assert not list(path.parent.glob('.download-*'))


def test_bad_download_preserves_previously_verified_revision(cfg, monkeypatch):
    data=b'#!/bin/sh\nexit 0\n'
    item=row(data)
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw: BytesIO(data))
    old=custom._fetch_script_to_cache(cfg,item)
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw: BytesIO(b'tampered'))
    with pytest.raises(RuntimeError,match='checksum mismatch'):
        custom._fetch_script_to_cache(cfg,item)
    assert old.read_bytes()==data
    assert not list(old.parent.glob('.download-*'))


def test_different_script_revision_does_not_replace_running_revision_path(cfg, monkeypatch):
    first=b'#!/bin/sh\necho first\n'
    second=b'#!/bin/sh\necho second\n'
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw: BytesIO(first))
    first_path=custom._fetch_script_to_cache(cfg,row(first))
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw: BytesIO(second))
    second_path=custom._fetch_script_to_cache(cfg,row(second))
    assert first_path != second_path
    assert first_path.read_bytes()==first
    assert second_path.read_bytes()==second


def test_matching_job_pin_uses_existing_direct_executor_without_changing_arguments(cfg, monkeypatch):
    data=b'external scenario'
    item=row(data)
    monkeypatch.setattr(custom,'fetch_custom_manifest',lambda *a,**kw: ([item],'network'))
    monkeypatch.setattr(custom,'_fetch_script_to_cache',lambda *a: Path('/cache/verified.py'))
    calls=[]
    monkeypatch.setattr(custom,'_run_direct',lambda path,args,**kw: calls.append((path,args,kw)) or (0,'{"status":"ok"}'))
    args=['--config','/opt/mcd/etc/mcd.toml','--job-id','job-1']
    result=custom.run_custom_script_by_key(cfg,script_key=item['key'],args=args,expected_sha256=item['sha256'],detach=False,live_output=False)
    assert result==(0,'{"status":"ok"}')
    assert calls==[(Path('/cache/verified.py'),args,{'live_output':False})]
