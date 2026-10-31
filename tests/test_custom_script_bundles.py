from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import hashlib
import tarfile
import pytest
from mcd_agent import custom_scripts as custom


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(custom_cache_dir=str(tmp_path/'cache'),custom_repo_base_url='https://controller.example.org',plugins_repo_base_url='',mcc_url='',custom_manifest_path='custom/manifest.json',mcc_token='existing-key',custom_run_mode_default='direct')


def bundle(*, name='scenarios/debian-family/skeleton.py', kind=None):
    out=BytesIO()
    with tarfile.open(fileobj=out,mode='w:gz') as archive:
        for path,data in [(name,b'#!/usr/bin/env python3\nprint("ok")\n'),('profiles/default.json',b'{"choice":"default"}')]:
            member=tarfile.TarInfo(path);member.size=len(data);member.mode=0o644
            if kind and path==name:
                member.type=kind;member.linkname='/outside'
                archive.addfile(member)
            else:archive.addfile(member,BytesIO(data))
    return out.getvalue()


def item(data,entrypoint='scenarios/debian-family/skeleton.py'):
    return {'key':'bundle-r1','file':'bundles/r1.tar.gz','sha256':hashlib.sha256(data).hexdigest(),'entrypoint':entrypoint,'run_mode':'direct','interactive':False}


def test_bundle_parser_preserves_additive_entrypoint():
    data=bundle();row=custom._parse_manifest({'items':[item(data)]})[0]
    assert row['entrypoint']=='scenarios/debian-family/skeleton.py'


def test_verified_bundle_retains_profiles_and_uses_isolated_content_addressed_directory(cfg,monkeypatch):
    data=bundle();row=item(data)
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:BytesIO(data))
    first=custom._fetch_script_to_cache(cfg,row)
    second=custom._fetch_script_to_cache(cfg,row)
    assert row['sha256'] in first.parts
    assert first!=second
    assert first.read_bytes()==second.read_bytes()
    root=first.parents[2]
    assert (root/'profiles/default.json').read_text()=='{"choice":"default"}'
    assert first.stat().st_mode & 0o100


@pytest.mark.parametrize('name,kind',[('../escape.py',None),('/outside',None),('scenarios/linked.py',tarfile.SYMTYPE),('scenarios/hard.py',tarfile.LNKTYPE)])
def test_unsafe_archive_is_never_activated(cfg,monkeypatch,name,kind):
    data=bundle(name=name,kind=kind)
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:BytesIO(data))
    with pytest.raises(RuntimeError):custom._fetch_script_to_cache(cfg,item(data))
    assert not list(Path(cfg.custom_cache_dir).rglob('run-*'))
    assert not list(Path(cfg.custom_cache_dir).rglob('.extract-*'))


@pytest.mark.parametrize('entrypoint',['../escape','/outside','missing.py','profiles'])
def test_bad_or_missing_entrypoint_cannot_execute(cfg,monkeypatch,entrypoint):
    data=bundle()
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:BytesIO(data))
    with pytest.raises(RuntimeError):custom._fetch_script_to_cache(cfg,item(data,entrypoint))


def test_bundle_checksum_failure_never_extracts(cfg,monkeypatch):
    data=bundle();row=item(data);row['sha256']='0'*64
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:BytesIO(data))
    monkeypatch.setattr(custom,'_activate_bundle',lambda *a:pytest.fail('unverified bytes must not extract'))
    with pytest.raises(RuntimeError,match='checksum mismatch'):custom._fetch_script_to_cache(cfg,row)


def test_bundle_execution_uses_same_entrypoint_arguments_and_bundle_working_directory(cfg,monkeypatch):
    data=bundle();row=item(data)
    monkeypatch.setattr(custom,'fetch_custom_manifest',lambda *a,**kw:([row],'network'))
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:BytesIO(data))
    calls=[]
    monkeypatch.setattr(custom,'_run_direct',lambda path,args,**kw:calls.append((path,args,kw)) or (0,'ok'))
    args=['--composition','profiles/default.json','--selection','default']
    assert custom.run_custom_script_by_key(cfg,script_key=row['key'],args=args,expected_sha256=row['sha256'],detach=False,live_output=False)==(0,'ok')
    path,actual_args,options=calls[0]
    assert actual_args==args
    assert path.relative_to(options['cwd']).as_posix()==row['entrypoint']
    assert (options['cwd']/'profiles/default.json').is_file()


def test_complete_manual_bundle_runs_standalone_without_repository_url(cfg,monkeypatch):
    import json
    data=bundle();row=item(data)
    cfg.custom_repo_base_url='';cfg.plugins_repo_base_url='';cfg.mcc_url=''
    cache=Path(cfg.custom_cache_dir);cache.mkdir()
    (cache/'manifest.json').write_text(json.dumps({'items':[row]}))
    target=cache/'downloads'/row['key']/f"{row['key']}-{row['sha256']}.tar.gz"
    target.parent.mkdir(parents=True);target.write_bytes(data)
    monkeypatch.setattr(custom.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('complete offline bundle requires no network'))
    assert custom.run_custom_script_by_key(cfg,script_key=row['key'],expected_sha256=row['sha256'],detach=False,live_output=False)==(0,'ok')
