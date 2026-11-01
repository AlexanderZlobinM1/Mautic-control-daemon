"""Migration goldens and fail-closed behavior after embedded preset retirement."""
import json
from pathlib import Path

import pytest

from mcd_agent import config
from mcd_agent.custom_scripts import CustomSelectedTaskError
from mcd_agent.hardware_profile import recommended_profile


@pytest.mark.parametrize('name', ['passive', 'tiny', 'mini', 'midi', 'maxi', 'hiload', 'ultra', 'farm', 'farm-ultra'])
def test_named_profile_without_selected_data_never_falls_back(tmp_path, monkeypatch, name):
    monkeypatch.setattr(config, 'DEFAULT_RUNTIME_ARTIFACT_DIR', str(tmp_path / 'no-runtime'))
    path = tmp_path / 'mcd.toml'; path.write_text('[profile]\nname=' + json.dumps(name) + '\n')
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    with pytest.raises(CustomSelectedTaskError) as error:
        config.load_config(str(path), allow_recover_from_mcc=False)
    assert error.value.code == 'custom_selected_task_artifact_missing_or_corrupt'
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert list(tmp_path.iterdir()) == [path]


def test_operator_custom_values_do_not_require_a_named_preset(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DEFAULT_RUNTIME_ARTIFACT_DIR', str(tmp_path / 'no-runtime'))
    path = tmp_path / 'mcd.toml'; path.write_text('[profile]\nname="custom"\n[runtime]\nscheduler_host_max_parallel=137\n')
    loaded = config.load_config(str(path), allow_recover_from_mcc=False)
    assert loaded.scheduler_host_max_parallel == 137
    with pytest.raises(CustomSelectedTaskError):
        recommended_profile(cpu_count=2, memory_kib=4*1024*1024, config_path=str(path))


def test_all_profiles_keep_frozen_values_and_inherited_operator_settings(installed_workload_data, tmp_path):
    frozen = json.loads((Path(__file__).parent / 'fixtures/universal-workload/profile-goldens.json').read_text())
    for entry in frozen['profiles']:
        path = tmp_path / (entry['profile_id'] + '.toml')
        path.write_text('[profile]\nname=' + json.dumps(entry['profile_id']) +
                        '\n[runtime]\njobs_max_workers=17\nsegment_retry_count=9\n')
        loaded = config.load_config(str(path), allow_recover_from_mcc=False)
        actual = {key: getattr(loaded, key) for key in entry['values']}
        assert json.dumps(actual, sort_keys=True) == json.dumps(entry['values'], sort_keys=True)
        assert loaded.jobs_max_workers == 17 and loaded.segment_retry_count == 9


def test_external_recommendations_match_204_pre_retirement_boundary_pairs(installed_workload_data):
    from mcd_agent.hardware_profile import recommended_farm_profile
    frozen = json.loads((Path(__file__).parent / 'fixtures/universal-workload/recommendation-goldens.json').read_text())
    assert len(frozen['cases']) == 204
    for row in frozen['cases']:
        args = {'cpu_count': row['cpu_count'], 'memory_kib': row['memory_kib'],
                'config_path': installed_workload_data['config_path']}
        assert recommended_profile(**args) == row['profile']
        assert recommended_farm_profile(**args) == row['farm_profile']


def new_catalog_fixture(tmp_path):
    """A distinct complete selected catalog with a name absent from the old one."""
    import hashlib
    import io
    import tarfile
    from test_universal_workload import resource_fixture
    _, selected, cached = resource_fixture(tmp_path)
    with tarfile.open(cached) as archive:
        members = {m.name: archive.extractfile(m).read() for m in archive if m.isfile()}
    root = selected['resource']['resource_root']
    profile = json.loads(members[root + '/profiles/large.json']); profile['profile_id'] = 'new-large'
    raw = (json.dumps(profile, sort_keys=True) + '\n').encode()
    members[root + '/profiles/large.json'] = raw
    pin = lambda path, data: {'path': path, 'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)}
    catalog = json.loads(members[root + '/catalog.json'])
    catalog['profiles'] = {'new-large': pin('profiles/large.json', raw)}
    members[root + '/catalog.json'] = (json.dumps(catalog, sort_keys=True) + '\n').encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name, data in members.items():
            member = tarfile.TarInfo(name); member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    raw = stream.getvalue()
    ref = {'schema': 'mcc-universal-workload-resource-ref-v1', 'key': 'new-catalog',
           'file': 'resources/new-catalog.tar.gz', 'sha256': hashlib.sha256(raw).hexdigest(),
           'size_bytes': len(raw), 'resource_root': root,
           'catalog': pin(root + '/catalog.json', members[root + '/catalog.json']),
           'files': [pin(name, data) for name, data in members.items()]}
    return ref, raw


def test_new_ref_and_new_name_admitted_and_persisted_as_one_pair(installed_workload_data, tmp_path, monkeypatch):
    import os
    import tomllib
    from mcd_agent import daemon
    from mcd_agent.universal_workload import preseed_universal_workload_resource
    path = Path(installed_workload_data['config_path']); path.write_text('[profile]\nname="passive"\n')
    old = config.load_config(str(path), allow_recover_from_mcc=False)
    ref, raw = new_catalog_fixture(tmp_path)
    preseed_universal_workload_resource(old, ref, raw)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    candidate = config.prepare_profile_selection(old, 'new-large', {'universal_workload_resource_ref': ref})
    assert candidate.profile_name == 'new-large' and candidate.scheduler_host_max_parallel == 123
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    replacements = []; original = os.replace
    def observe(source, destination):
        if Path(destination) == path:
            value = tomllib.loads(Path(source).read_text())
            assert value['profile']['name'] == 'new-large'
            assert value['runtime']['universal_workload_resource_ref'] == ref
            replacements.append(str(destination))
        return original(source, destination)
    monkeypatch.setattr(os, 'replace', observe)
    daemon._persist_resolved_runtime_to_config(old, {}, {'universal_workload_resource_ref': ref}, {},
                                              full_snapshot=True, profile_name='new-large')
    assert replacements == [str(path)]
    loaded = config.load_config(str(path), allow_recover_from_mcc=False)
    assert loaded.profile_name == 'new-large' and loaded.universal_workload_resource_ref == ref
    assert loaded.scheduler_host_max_parallel == 123


def test_invalid_new_ref_cannot_persist_half_a_profile_pair(installed_workload_data, tmp_path):
    from mcd_agent import daemon
    path = Path(installed_workload_data['config_path']); path.write_text('[profile]\nname="passive"\n[mcc]\nurl=""\n')
    old = config.load_config(str(path), allow_recover_from_mcc=False)
    from dataclasses import replace
    old = replace(old, mcc_url=None, custom_repo_base_url="", plugins_repo_base_url="")
    ref, _ = new_catalog_fixture(tmp_path)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    with pytest.raises(CustomSelectedTaskError):
        daemon._persist_resolved_runtime_to_config(old, {}, {'universal_workload_resource_ref': ref}, {},
                                                  full_snapshot=True, profile_name='new-large')
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_legacy_profile_is_readable_but_not_a_new_public_selection(installed_workload_data):
    from mcd_agent.mode import supported_profile_names
    path = Path(installed_workload_data['config_path'])
    assert 'farm' not in supported_profile_names(str(path))
    path.write_text('[profile]\nname="farm"\n')
    old = config.load_config(str(path), allow_recover_from_mcc=False)
    assert config.prepare_profile_selection(old, 'farm', {}).scheduler_host_max_parallel == 12
    path.write_text('[profile]\nname="custom"\n')
    custom = config.load_config(str(path), allow_recover_from_mcc=False)
    with pytest.raises(ValueError):
        config.prepare_profile_selection(custom, 'farm', {})


def test_atomic_profile_writer_preserves_symlink_target_mode_and_stable_bytes(tmp_path):
    import stat
    target = tmp_path / 'actual.toml'; target.write_text('[profile]\nname="custom"\n')
    target.chmod(0o640); link = tmp_path / 'entry.toml'; link.symlink_to(target.name)
    config.persist_profile_runtime_values(str(link), 'custom', {'jobs_max_workers': 17}, set())
    assert link.is_symlink() and 'jobs_max_workers = 17' in target.read_text()
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    before = (target.read_bytes(), target.stat().st_mtime_ns)
    assert config.persist_profile_runtime_values(str(link), 'custom', {'jobs_max_workers': 17}, set())[1] is False
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before


def test_failed_atomic_profile_replace_preserves_original_and_removes_temporary(tmp_path, monkeypatch):
    import os
    target = tmp_path / 'mcd.toml'; target.write_text('[profile]\nname="custom"\n')
    before = (target.read_bytes(), target.stat().st_mtime_ns)
    def fail(*args):
        raise PermissionError('injected filesystem replacement failure')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(PermissionError):
        config.persist_profile_runtime_values(str(target), 'new-large', {'jobs_max_workers': 17}, set())
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before
    assert list(tmp_path.iterdir()) == [target]


def test_startup_one_sync_accepts_new_name_only_in_new_verified_catalog(installed_workload_data, tmp_path, monkeypatch):
    from dataclasses import replace
    from mcd_agent import daemon
    from mcd_agent.universal_workload import preseed_universal_workload_resource
    path = Path(installed_workload_data['config_path']); path.write_text('[profile]\nname="passive"\n')
    old = config.load_config(str(path), allow_recover_from_mcc=False)
    old = replace(old, mcc_url='https://fixture.invalid', mcc_token='synthetic-unit-fixture')
    ref, raw = new_catalog_fixture(tmp_path)
    preseed_universal_workload_resource(old, ref, raw)
    response = {'status': 'ok', 'desired_profile': 'new-large',
                'runtime_overrides': {'universal_workload_resource_ref': ref},
                'desired_state': {'revision': 1, 'updated_at': '2026-11-01T12:00:00Z'}}
    monkeypatch.setattr(daemon, 'fetch_runtime_overrides', lambda *args, **kwargs: response)
    store = daemon.TaskStore(str(tmp_path / 'startup.sqlite'))
    try:
        assert daemon._bootstrap_remote_runtime_before_local_baseline(old, store, []) == 'applied'
        loaded = config.load_config(str(path), allow_recover_from_mcc=False)
        assert loaded.profile_name == 'new-large' and loaded.universal_workload_resource_ref == ref
        assert loaded.scheduler_host_max_parallel == 123
    finally:
        store.conn.close()
