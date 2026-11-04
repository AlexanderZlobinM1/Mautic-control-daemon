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


def resource_fixture(tmp_path, *, omit=None, large_slots=123):
    root = 'profiles/example/7'
    def encode(value):
        return (json.dumps(value, sort_keys=True) + '\n').encode()
    def pin(path, raw):
        return {'path': path, 'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw)}
    profile = encode({'profile_id': 'large', 'values': {'scheduler_host_max_parallel': large_slots},
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


def runtime_resource_fixture(tmp_path):
    cfg, selected, cached = resource_fixture(tmp_path)
    ref = {key: selected[key] for key in ('key', 'file', 'sha256')}
    ref.update(schema='mcc-universal-workload-resource-ref-v1', size_bytes=len(cached.read_bytes()),
               **selected['resource'])
    stage = tmp_path / 'runtime'
    for name in ('__init__', 'daemon', 'state_push', 'mcd_control'):
        p = stage / ('mcd_agent/' + name + '.py'); p.parent.mkdir(parents=True, exist_ok=True); p.write_text('# ready test payload\n')
    resource = stage / ref['file']; resource.parent.mkdir(parents=True); resource.write_bytes(cached.read_bytes())
    (stage / 'requirements.txt').write_text('')
    (stage / 'wheel').mkdir(); (stage / 'wheel/fixture.whl').write_bytes(b'package installation is not part of preseed')
    manifest = {'schema': 'mcd-runtime-package-v1', 'version': '7.0.0', 'wheel': 'wheel/fixture.whl',
                'requirements': 'requirements.txt', 'universal_workload_resource_ref': ref,
                'payload_sha256': {p.relative_to(stage).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in stage.rglob('*') if p.is_file()}}
    (stage / 'mcd-runtime-manifest.json').write_text(json.dumps(manifest))
    return cfg, ref, cached, stage


def test_runtime_preseed_before_configuration_uses_verified_payload_and_existing_cache(tmp_path, monkeypatch):
    from mcd_agent.universal_workload import preseed_runtime_workload_resources
    from mcd_agent import config as config_module
    cfg, ref, cached, stage = runtime_resource_fixture(tmp_path)
    forbid_network_and_execution(monkeypatch)
    monkeypatch.setattr(config_module, 'load_config', lambda *a, **kw: pytest.fail('preseed loaded configuration'))
    cache_dir = tmp_path / 'fresh-cache'
    digest = hashlib.sha256((stage / 'mcd-runtime-manifest.json').read_bytes()).hexdigest()
    result = preseed_runtime_workload_resources(runtime_dir=stage, cache_dir=cache_dir, expected_manifest_sha256=digest)
    assert result['status'] == 'ok' and result['key'] == ref['key']
    assert result['size_bytes'] == ref['size_bytes']
    from pathlib import Path
    target = Path(result['cache_path']); assert target.read_bytes() == cached.read_bytes()
    before = (target.stat().st_mtime_ns, target.stat().st_mode)
    preseed_runtime_workload_resources(runtime_dir=stage, cache_dir=cache_dir, expected_manifest_sha256=digest)
    assert (target.stat().st_mtime_ns, target.stat().st_mode) == before
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_corrupt_embedded_required_data_fails_before_cache_mutation(tmp_path, monkeypatch):
    from mcd_agent.universal_workload import preseed_runtime_workload_resources
    _, ref, _, stage = runtime_resource_fixture(tmp_path)
    (stage / ref['file']).write_bytes(b'corrupt required artifact')
    forbid_network_and_execution(monkeypatch)
    target = tmp_path / 'fresh-cache'
    with pytest.raises(RuntimeError):
        preseed_runtime_workload_resources(runtime_dir=stage, cache_dir=target)
    assert not target.exists()


def test_runtime_preseed_repairs_corrupt_selected_cache_from_same_embedded_digest(tmp_path, monkeypatch):
    from mcd_agent.universal_workload import preseed_runtime_workload_resources
    from pathlib import Path

    _, ref, expected, stage = runtime_resource_fixture(tmp_path)
    forbid_network_and_execution(monkeypatch)
    cache_dir = tmp_path / "cache"
    first = preseed_runtime_workload_resources(runtime_dir=stage, cache_dir=cache_dir)
    cached = Path(first["cache_path"])
    cached.write_bytes(b"corrupt selected cache")

    repaired = preseed_runtime_workload_resources(runtime_dir=stage, cache_dir=cache_dir)

    assert repaired["sha256"] == ref["sha256"]
    assert Path(repaired["cache_path"]).read_bytes() == expected.read_bytes()
    assert hashlib.sha256(Path(repaired["cache_path"]).read_bytes()).hexdigest() == ref["sha256"]


def test_completed_replay_verifies_only_and_never_repairs_missing_cache(tmp_path, monkeypatch):
    from mcd_agent.universal_workload import preseed_runtime_workload_resources
    _, _, _, stage = runtime_resource_fixture(tmp_path)
    target = tmp_path / 'not-created-cache'
    forbid_network_and_execution(monkeypatch)
    with pytest.raises(RuntimeError):
        preseed_runtime_workload_resources(runtime_dir=stage, cache_dir=target, verify_only=True)
    assert not target.exists()
    result = preseed_runtime_workload_resources(runtime_dir=stage, cache_dir=target)
    from pathlib import Path
    cached = Path(result['cache_path']); before = (cached.read_bytes(), cached.stat().st_mtime_ns, cached.stat().st_mode)
    preseed_runtime_workload_resources(runtime_dir=stage, cache_dir=target, verify_only=True)
    assert (cached.read_bytes(), cached.stat().st_mtime_ns, cached.stat().st_mode) == before


def test_selected_data_resource_survives_existing_cache_gc_when_latest_omits_it(tmp_path):
    import os
    cfg, ref, cached, _ = runtime_resource_fixture(tmp_path)
    cfg.universal_workload_resource_ref = ref
    cfg.custom_logs_keep_days = 1; cfg.custom_logs_max_files = 1
    cfg.custom_downloads_keep_days = 1; cfg.custom_downloads_max_entries = 1
    stale = cached.parent.parent / 'unselected-old'
    stale.mkdir(); (stale / 'unused').write_bytes(b'old')
    for path in (cached.parent, stale): os.utime(path, (1, 1))
    custom_scripts.cleanup_custom_cache(cfg, known_keys={'only-new-remote-script'})
    assert cached.is_file() and not stale.exists()


def configuration(path, *, cache_dir, ref=None, include_ref=False):
    from mcd_agent.config import _toml_literal
    path.write_text('[profile]\nname="large"\n[runtime]\nstate_db_path=' + _toml_literal(str(path.parent / 'state/db.sqlite'))
                    + '\njobs_max_workers=17\n'
                    + ('universal_workload_resource_ref=' + _toml_literal(ref) + '\n' if include_ref else '')
                    + '[custom]\ncache_dir=' + _toml_literal(str(cache_dir)) + '\n')


def test_initial_runtime_manifest_preseeds_before_named_profile_apply_without_config_rewrite(tmp_path, monkeypatch):
    from mcd_agent import config as config_module
    _, ref, _, stage = runtime_resource_fixture(tmp_path)
    monkeypatch.setattr(config_module, 'DEFAULT_RUNTIME_ARTIFACT_DIR', str(stage))
    path = tmp_path / 'mcd.toml'; cache = tmp_path / 'new-cache'
    configuration(path, cache_dir=cache)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    forbid_network_and_execution(monkeypatch)
    cfg = config_module.load_config(str(path), allow_recover_from_mcc=False)
    assert cfg.universal_workload_resource_ref == ref
    assert cfg.scheduler_host_max_parallel == 123
    assert cfg.jobs_max_workers == 17
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_explicit_manual_ref_works_without_parent_runtime_manifest_and_is_in_generic_settings(tmp_path, monkeypatch):
    from mcd_agent import config as config_module
    cfg_cache, ref, _, _ = runtime_resource_fixture(tmp_path)
    monkeypatch.setattr(config_module, 'DEFAULT_RUNTIME_ARTIFACT_DIR', str(tmp_path / 'no-runtime-artifact'))
    path = tmp_path / 'mcd.toml'; configuration(path, cache_dir=cfg_cache.custom_cache_dir, ref=ref, include_ref=True)
    forbid_network_and_execution(monkeypatch)
    cfg = config_module.load_config(str(path), allow_recover_from_mcc=False)
    assert cfg.scheduler_host_max_parallel == 123 and cfg.jobs_max_workers == 17
    assert config_module.runtime_effective_map(cfg)['universal_workload_resource_ref'] == ref
    assert config_module.runtime_overrides_from_config_file(str(path))['universal_workload_resource_ref'] == ref


def test_newer_selected_ref_rebases_same_profile_and_cannot_fall_back_to_initial_data(tmp_path, monkeypatch):
    from mcd_agent import config as config_module
    cfg_cache, ref, _, stage = runtime_resource_fixture(tmp_path)
    monkeypatch.setattr(config_module, 'DEFAULT_RUNTIME_ARTIFACT_DIR', str(stage))
    path = tmp_path / 'mcd.toml'; configuration(path, cache_dir=cfg_cache.custom_cache_dir, ref=ref, include_ref=True)
    cfg = config_module.load_config(str(path), allow_recover_from_mcc=False)
    from dataclasses import replace
    cfg = replace(cfg, custom_repo_base_url='', plugins_repo_base_url='')
    other = tmp_path / 'other'; other.mkdir()
    _, selected, cached2 = resource_fixture(other, large_slots=257)
    new_ref = {key: selected[key] for key in ('key', 'file', 'sha256')}
    new_ref.update(schema='mcc-universal-workload-resource-ref-v1', size_bytes=len(cached2.read_bytes()), **selected['resource'])
    forbid_network_and_execution(monkeypatch)
    with pytest.raises(RuntimeError):
        config_module.apply_runtime_overrides(cfg, {'universal_workload_resource_ref': new_ref})
    assert cfg.scheduler_host_max_parallel == 123 and cfg.universal_workload_resource_ref == ref
    from pathlib import Path
    manual = Path(cfg_cache.custom_cache_dir) / 'downloads' / new_ref['key'] / (new_ref['key'] + '-' + new_ref['sha256'] + '.tar.gz')
    manual.write_bytes(cached2.read_bytes())
    updated, applied, unsupported, blocked = config_module.apply_runtime_overrides(cfg, {'universal_workload_resource_ref': new_ref})
    assert updated.scheduler_host_max_parallel == 257 and updated.jobs_max_workers == 17
    assert updated.universal_workload_resource_ref == new_ref
    assert applied == ['universal_workload_resource_ref'] and not unsupported and not blocked


def test_selected_cache_error_exposes_stable_public_code_without_arbitrary_message():
    expected = 'custom_selected_task_artifact_missing_or_corrupt'
    assert custom_scripts.CustomSelectedTaskError(expected).code == expected
    assert custom_scripts.CustomSelectedTaskError('untrusted free text / path').code == 'custom_selected_task_failed'
