"""Data consumers must not become script execution or hardware clamps."""
import hashlib
import io
import json
import stat
import tarfile

import pytest

from mcd_agent import custom_scripts
from mcd_agent.universal_workload import (
    load_universal_workload_resource, recommend_workload_profile, workload_profile_values,
)


def resource_fixture(tmp_path, *, omit=None):
    root = 'profiles/example/7'
    def encode(value):
        return (json.dumps(value, sort_keys=True) + '\n').encode()
    def pin(path, raw):
        return {'path': path, 'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw)}
    profile = encode({'profile_id': 'large', 'values': {'scheduler_host_max_parallel': 123},
                      'inherited_global_runtime_defaults': {'jobs_max_workers': 2}})
    hardware = encode({'rows': [{'bounds': {'cpu_count': {'max_inclusive': 2}},
                                'recommended_profile': 'small', 'recommended_farm_profile': 'farm-small'},
                               {'bounds': {}, 'recommended_profile': 'large',
                                'recommended_farm_profile': 'large'}]})
    catalog = encode({'custom': {'profile_id': 'custom', 'operator_owned': True},
                      'profile_owned_fields': ['scheduler_host_max_parallel'],
                      'profiles': {'large': pin('profiles/large.json', profile), 'small': {}, 'farm-small': {}},
                      'hardware_recommendations': pin('hardware.json', hardware)})
    files = {root + '/catalog.json': catalog, root + '/profiles/large.json': profile,
             root + '/hardware.json': hardware}
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name, raw in files.items():
            if name == omit:
                continue
            member = tarfile.TarInfo(name); member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    data = stream.getvalue()
    selected = {'key': 'selected-data', 'kind': 'data_only_no_entrypoint', 'entrypoint': None,
                'active': False, 'state': 'candidate', 'file': 'resources/selected.tar.gz',
                'sha256': hashlib.sha256(data).hexdigest(),
                'resource': {'resource_root': root, 'catalog': pin(root + '/catalog.json', catalog),
                             'files': [pin(name, raw) for name, raw in files.items()]}}
    cfg = custom_scripts.standalone_custom_cache_config(tmp_path / 'cache')
    cached = tmp_path / 'cache/downloads/selected-data' / ('selected-data-' + selected['sha256'] + '.tar.gz')
    cached.parent.mkdir(parents=True); cached.write_bytes(data); cached.chmod(0o600)
    return cfg, selected, cached


def forbid_network_and_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('data-only exact cache must not access transport or execute a process')
    monkeypatch.setattr(custom_scripts.urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(custom_scripts.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(custom_scripts.subprocess, 'run', forbidden)


def test_offline_inactive_manual_resource_has_no_transport_or_process_and_preserves_file(tmp_path, monkeypatch):
    cfg, selected, cached = resource_fixture(tmp_path)
    before = (cached.read_bytes(), cached.stat().st_mtime_ns, stat.S_IMODE(cached.stat().st_mode))
    forbid_network_and_execution(monkeypatch)
    resource = load_universal_workload_resource(cfg, selected)
    assert recommend_workload_profile(resource, cpu_count=1, memory_kib=0) == 'small'
    assert recommend_workload_profile(resource, cpu_count=1, memory_kib=0, farm=True) == 'farm-small'
    # Manual selection stays literal even when the hardware recommendation differs.
    assert workload_profile_values(resource, 'large') == {'scheduler_host_max_parallel': 123}
    assert workload_profile_values(resource, 'custom') == {}
    assert (cached.read_bytes(), cached.stat().st_mtime_ns, stat.S_IMODE(cached.stat().st_mode)) == before


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'member-pin', 'missing-member', 'entrypoint'])
def test_invalid_required_resource_fails_before_execution_or_transport(tmp_path, monkeypatch, damage):
    cfg, selected, cached = resource_fixture(tmp_path, omit='profiles/example/7/hardware.json' if damage == 'missing-member' else None)
    if damage == 'missing': cached.unlink()
    if damage == 'corrupt': cached.write_bytes(b'corrupt')
    if damage == 'member-pin': selected['resource']['files'][-1]['sha256'] = 'a' * 64
    if damage == 'entrypoint': selected['entrypoint'] = 'run.py'
    forbid_network_and_execution(monkeypatch)
    with pytest.raises(RuntimeError): load_universal_workload_resource(cfg, selected)


def test_data_download_reuses_existing_transport_without_executable_permissions(tmp_path, monkeypatch):
    cfg, selected, cached = resource_fixture(tmp_path)
    raw = cached.read_bytes(); cached.unlink()
    cfg.custom_repo_base_url = 'https://qa.invalid'
    requests = []
    def download(request, **kwargs):
        requests.append(request.full_url)
        return io.BytesIO(raw)
    monkeypatch.setattr(custom_scripts.urllib.request, 'urlopen', download)
    resource = load_universal_workload_resource(cfg, selected, offline_only=False)
    assert workload_profile_values(resource, 'large')['scheduler_host_max_parallel'] == 123
    assert len(requests) == 1 and stat.S_IMODE(cached.stat().st_mode) == 0o600
    load_universal_workload_resource(cfg, selected, offline_only=False)
    assert len(requests) == 1
