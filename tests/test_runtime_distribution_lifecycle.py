"""Real pip ownership behavior, independent of MCD source-tree imports."""
import base64
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from mcd_agent import self_update


def bridge_module():
    path=Path(__file__).parents[1]/'tools/mcd_runtime_bootstrap.py'
    spec=importlib.util.spec_from_file_location('runtime_ownership_bridge_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def stage_wheel(root,version,*,retired=False,new_only=False,import_version=None):
    root.mkdir();package={'mcd_agent/__init__.py':f'__version__ = "{import_version or version}"\n'.encode()}
    for name in ['daemon','state_push','mcd_control']:package['mcd_agent/'+name+'.py']=b'# synthetic package-lifecycle fixture\n'
    if retired:package['mcd_agent/templates/nginx/instance_migrate_vhost.conf']=b'# known old distribution-owned resource\n'
    if new_only:package['mcd_agent/new-owned.json']=b'{}\n'
    metadata='mcd_agent-'+version+'.dist-info'
    contents={**package,metadata+'/METADATA':f'Metadata-Version: 2.1\nName: mcd-agent\nVersion: {version}\n'.encode(),metadata+'/WHEEL':b'Wheel-Version: 1.0\nGenerator: ownership-qa\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}
    record=io.StringIO();writer=csv.writer(record,lineterminator='\n')
    for name,data in sorted(contents.items()):writer.writerow([name,'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('='),len(data)])
    writer.writerow([metadata+'/RECORD','','']);contents[metadata+'/RECORD']=record.getvalue().encode()
    wheel=root/'wheel'/('mcd_agent-'+version+'-py3-none-any.whl');wheel.parent.mkdir()
    with zipfile.ZipFile(wheel,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in contents.items():archive.writestr(name,data)
    for name,data in package.items():
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    (root/'requirements.txt').write_text('')
    manifest={'schema':'mcd-runtime-package-v1','version':version,'wheel':wheel.relative_to(root).as_posix(),'requirements':'requirements.txt','payload_sha256':{p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}}
    (root/'mcd-runtime-manifest.json').write_text(json.dumps(manifest)+'\n');return manifest


@pytest.fixture
def installation(tmp_path):
    install=tmp_path/'installed';install.mkdir();subprocess.run([sys.executable,'-m','venv',str(install/'venv')],check=True,capture_output=True)
    p=install/'etc/mcd.toml';p.parent.mkdir();p.write_text('[profile]\nname="custom"\n');return install


def install_stage(backend,install,stage):
    if backend=='agent':self_update._install_agent_package_for_source(install,stage)
    else:bridge_module()._install_runtime(install,stage,json.loads((stage/'mcd-runtime-manifest.json').read_text()))


def package_path(install):
    result=subprocess.run([str(install/'venv/bin/python'),'-c','import mcd_agent;print(mcd_agent.__file__)'],cwd='/',env=self_update._isolated_venv_env(),check=True,capture_output=True,text=True)
    return Path(result.stdout.strip()).parent


@pytest.mark.parametrize('backend',['agent','bridge'])
def test_clean_install_uses_only_installed_package(installation,tmp_path,backend):
    stage=tmp_path/'stage';stage_wheel(stage,'1.7.0',new_only=True);install_stage(backend,installation,stage)
    package=package_path(installation);assert '/site-packages/' in str(package) and package!=stage/'mcd_agent';assert (package/'new-owned.json').is_file()


@pytest.mark.parametrize('backend',['agent','bridge'])
def test_owned_resource_retired_on_upgrade_repeat_and_package_rollback_preserve_foreign_data(installation,tmp_path,backend):
    old=tmp_path/'old';new=tmp_path/'new';stage_wheel(old,'1.6.38',retired=True);stage_wheel(new,'1.7.0',new_only=True)
    install_stage(backend,installation,old);package=package_path(installation);retired=package/'templates/nginx/instance_migrate_vhost.conf';assert retired.is_file()
    foreign=package/'foreign-operator.keep';foreign.write_bytes(b'unlisted operator data');config=installation/'etc/mcd.toml';before=(config.read_bytes(),config.stat().st_mtime_ns)
    for _ in range(2):
        install_stage(backend,installation,new);assert not retired.exists();assert (package/'new-owned.json').is_file();assert foreign.read_bytes()==b'unlisted operator data';assert (config.read_bytes(),config.stat().st_mtime_ns)==before
    install_stage(backend,installation,old);assert retired.is_file() and not (package/'new-owned.json').exists();assert foreign.read_bytes()==b'unlisted operator data';assert (config.read_bytes(),config.stat().st_mtime_ns)==before


@pytest.mark.parametrize('backend',['agent','bridge'])
def test_post_install_verification_failure_can_restore_previous_owned_files(installation,tmp_path,backend):
    old=tmp_path/'old';bad=tmp_path/'bad';stage_wheel(old,'1.6.38',retired=True);stage_wheel(bad,'1.7.0',new_only=True,import_version='1.7.1')
    install_stage(backend,installation,old);package=package_path(installation);foreign=package/'foreign-operator.keep';foreign.write_bytes(b'preserve');config=installation/'etc/mcd.toml';before=config.read_bytes()
    with pytest.raises(RuntimeError,match='version mismatch'):install_stage(backend,installation,bad)
    install_stage(backend,installation,old);assert (package/'templates/nginx/instance_migrate_vhost.conf').is_file();assert not (package/'new-owned.json').exists();assert foreign.read_bytes()==b'preserve' and config.read_bytes()==before


@pytest.mark.parametrize('fail_successor', [False, True])
def test_ordinary_apply_never_uses_legacy_tree_and_retires_it_only_after_successor_verification(tmp_path, monkeypatch, fail_successor):
    from types import SimpleNamespace
    install = tmp_path / 'installed'
    install.mkdir()
    legacy = install / 'src'
    legacy.mkdir()
    (legacy / 'must-not-be-read').write_bytes(b'forbidden production source')
    stage_wheel(install / 'runtime', '1.6.38')
    cfg = SimpleNamespace(mcd_install_dir=str(install), config_file_path='', mcd_update_cleanup_enabled=False)
    installs = []
    for name in ('_read_state', 'capture_profile_authority'):
        monkeypatch.setattr(self_update, name, lambda *a, **kw: {})
    for name in ('restore_profile_authority', '_write_state', '_ensure_mcd_service_kill_mode',
                 'release_session', '_restart_service_async', '_release_update_lock'):
        monkeypatch.setattr(self_update, name, lambda *a, **kw: None)
    monkeypatch.setattr(self_update, '_defer_update_for_backup_lock', lambda *a, **kw: None)
    monkeypatch.setattr(self_update, 'installed_agent_version', lambda: '1.6.39')
    monkeypatch.setattr(self_update, '_cluster_update_enabled', lambda cfg: False)
    monkeypatch.setattr(self_update, '_ensure_update_archive', lambda *a: tmp_path / 'selected.tar.gz')
    monkeypatch.setattr(self_update, '_extract_archive_to_dir', lambda archive, dest: stage_wheel(dest, '1.6.40'))
    monkeypatch.setattr(self_update, '_install_requirements_for_staged_source', lambda *a: None)
    monkeypatch.setattr(self_update, '_pre_switch_smoke_check', lambda *a: None)
    def install_package(root, source):
        version = json.loads((source / 'mcd-runtime-manifest.json').read_text())['version']
        installs.append(version)
        assert legacy.is_dir(), 'legacy must survive until successor verification finishes'
        if version == '1.6.40' and fail_successor:
            raise RuntimeError('injected successor verification failure')
    monkeypatch.setattr(self_update, '_install_agent_package_for_source', install_package)
    ok, message = self_update._apply_update_locked(cfg, {'status': 'update', 'target': '1.6.40',
                                                        'package_url': 'https://qa.invalid/selected.tar.gz'}, None)
    if fail_successor:
        assert not ok and 'successor verification failure' in message
        assert legacy.is_dir() and (install / 'runtime').is_dir()
        assert json.loads((install / 'runtime/mcd-runtime-manifest.json').read_text())['version'] == '1.6.38'
        assert installs == ['1.6.40', '1.6.38']
    else:
        assert ok and (install / 'runtime').is_dir() and not legacy.exists()
        assert installs == ['1.6.40']
        assert not list((install / 'var/updates').glob('*legacy*'))


def test_runtime_include_migration_rewrites_only_managed_forbidden_defaults(tmp_path):
    install = tmp_path / 'installed'
    runtime_etc = install / 'runtime/etc'
    runtime_etc.mkdir(parents=True)
    for name in ('mcd-agent.system.example.toml', 'mcd-agent.operator.example.toml'):
        (runtime_etc / name).write_text('[runtime]\n')
    config = install / 'etc/mcd.toml'
    config.parent.mkdir(parents=True)
    foreign = install / 'etc/operator.toml'
    foreign.write_text('[runtime]\n')
    config.write_text(
        '[include]\nfiles = ['
        f'"{install}/src/etc/mcd-agent.system.example.toml", '
        f'"{install}/src/etc/mcd-agent.operator.example.toml", '
        f'"{foreign}"'
        ']\n'
    )
    result = self_update._migrate_forbidden_runtime_includes(str(config), install, install / 'runtime')
    assert result['changed'] is True
    updated = config.read_text()
    assert f'{install}/runtime/etc/mcd-agent.system.example.toml' in updated
    assert f'{install}/runtime/etc/mcd-agent.operator.example.toml' in updated
    assert str(foreign) in updated
    assert f'{install}/src/' not in updated
