from types import SimpleNamespace

import pytest

from mcd_agent import daemon, config
from mcd_agent.runtime_overrides import local_runtime_overrides
from test_universal_workload import runtime_resource_fixture


@pytest.mark.parametrize('winner', ['mcd', 'mcc'])
def test_resource_identity_uses_existing_setting_updated_at_not_artifact_identity(tmp_path, winner):
    _, ref, _, _ = runtime_resource_fixture(tmp_path)
    other = {**ref, 'key': 'different-artifact', 'sha256': 'a' * 64}
    local = daemon._runtime_metadata_empty(); remote = daemon._runtime_metadata_empty()
    key = 'universal_workload_resource_ref'
    daemon._runtime_metadata_set(local, 'host', key,
        daemon._runtime_metadata_stamp(source='mcd', updated_at=200, revision=3, event_id='mcd-edit'))
    daemon._runtime_metadata_set(remote, 'host', key,
        daemon._runtime_metadata_stamp(source='mcc', updated_at=300 if winner == 'mcc' else 100,
                                       revision=99, event_id='mcc-edit'))
    result = daemon._resolve_runtime_snapshots({key: ref}, {key: other}, local, remote)
    assert result['runtime'][key] == (other if winner == 'mcc' else ref)


def test_explicit_newer_resource_tombstone_is_distinct_from_reset_or_absence(tmp_path):
    _, ref, _, _ = runtime_resource_fixture(tmp_path)
    key = 'universal_workload_resource_ref'
    local = daemon._runtime_metadata_empty(); remote = daemon._runtime_metadata_empty()
    daemon._runtime_metadata_set(local, 'host', key,
        daemon._runtime_metadata_stamp(source='mcd', updated_at=200, revision=3, event_id='local-selection'))
    assert daemon._resolve_runtime_snapshots({key: ref}, {}, local, remote)['runtime'][key] == ref
    daemon._runtime_metadata_set(remote, 'host', key,
        daemon._runtime_metadata_stamp(source='mcc', updated_at=201, revision=4, event_id='clear-selection', tombstone=True))
    assert key not in daemon._resolve_runtime_snapshots({key: ref}, {}, local, remote)['runtime']


def test_loss_restores_resource_and_all_other_durable_settings_using_existing_store(tmp_path, monkeypatch):
    cfg_cache, ref, _, stage = runtime_resource_fixture(tmp_path)
    monkeypatch.setattr(config, 'DEFAULT_RUNTIME_ARTIFACT_DIR', str(stage))
    path = tmp_path / 'mcd.toml'
    path.write_text('[profile]\nname="custom"\n[runtime]\nsegment_priority_parallel_idle=1\n'
                    '[custom]\ncache_dir=' + config._toml_literal(cfg_cache.custom_cache_dir) + '\n')
    desired = {'universal_workload_resource_ref': ref, 'segment_priority_parallel_idle': 0,
               'scheduler_elastic_slots_enabled': False, 'db_watchdog': {},
               'plugin_operations': {str(tmp_path / 'instance'): [{'operation_key': 'stats', 'interval_sec': 600}]},
               'segment_full_scan_interval_sec': 47}
    store = daemon.TaskStore(str(tmp_path / 'durable.sqlite'))
    try:
        store.put_runtime_sync('local_runtime', {'segment_priority_parallel_idle': 1})
        store.put_runtime_sync('mcc_runtime', desired)
        restored, _ = daemon._initialize_local_runtime_baseline(SimpleNamespace(config_file_path=str(path)), store)
        assert restored == desired
        loaded = config.load_config(str(path), allow_recover_from_mcc=False)
        assert local_runtime_overrides(loaded) == desired
        assert loaded.universal_workload_resource_ref == ref
    finally:
        store.conn.close()


@pytest.mark.parametrize('previously_selected', [False, True])
def test_winning_clear_survives_restart_without_resurrecting_initial_package_reference(tmp_path, monkeypatch, previously_selected):
    from test_universal_workload import configuration
    cfg_cache, ref, _, stage = runtime_resource_fixture(tmp_path)
    monkeypatch.setattr(config, 'DEFAULT_RUNTIME_ARTIFACT_DIR', str(stage))
    path = tmp_path / 'mcd.toml'
    configuration(path, cache_dir=cfg_cache.custom_cache_dir, ref=ref, include_ref=previously_selected)
    path.write_text(path.read_text().replace('name="large"', 'name="custom"'))
    key = 'universal_workload_resource_ref'
    metadata = daemon._runtime_metadata_empty()
    daemon._runtime_metadata_set(metadata, 'host', key,
        daemon._runtime_metadata_stamp(source='mcc', updated_at=201, revision=4, event_id='explicit-clear', tombstone=True))
    import copy
    before_metadata = copy.deepcopy(metadata)
    daemon._persist_resolved_runtime_to_config(SimpleNamespace(config_file_path=str(path)),
        {key: ref} if previously_selected else {}, {}, metadata, full_snapshot=True)
    loaded = config.load_config(str(path), allow_recover_from_mcc=False)
    assert loaded.universal_workload_resource_ref == {}
    assert key not in config.runtime_effective_map(loaded)
    assert key not in local_runtime_overrides(loaded)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    daemon._persist_resolved_runtime_to_config(loaded, {}, {}, metadata, full_snapshot=True)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert metadata == before_metadata


def test_existing_nullable_cluster_index_can_clear_after_a_real_integer_value(tmp_path, monkeypatch):
    from test_universal_workload import configuration
    cfg_cache, ref, _, stage = runtime_resource_fixture(tmp_path)
    monkeypatch.setattr(config, 'DEFAULT_RUNTIME_ARTIFACT_DIR', str(stage))
    path = tmp_path / 'mcd.toml'; configuration(path, cache_dir=cfg_cache.custom_cache_dir, ref=ref, include_ref=True)
    path.write_text(path.read_text().replace('name="large"', 'name="custom"'))
    cfg = config.load_config(str(path), allow_recover_from_mcc=False)
    cfg, _, _, _ = config.apply_runtime_overrides(cfg, {'cluster_node_index': 1})
    assert cfg.cluster_node_index == 1
    cleared, applied, unsupported, blocked = config.apply_runtime_overrides(cfg, {'cluster_node_index': None})
    assert cleared.cluster_node_index is None and applied == ['cluster_node_index']
    assert not unsupported and not blocked


@pytest.mark.parametrize('key,value', [('cluster_node_index', 1), ('backup_cluster_authority_host', 'qa-backup.invalid'),
                                     ('mautic6_core_patch_version_min', '6.0.0')])
def test_nullable_settings_round_trip_clear_through_existing_toml_and_runtime_wire(tmp_path, monkeypatch, key, value):
    from test_universal_workload import configuration
    cfg_cache, ref, _, stage = runtime_resource_fixture(tmp_path)
    monkeypatch.setattr(config, 'DEFAULT_RUNTIME_ARTIFACT_DIR', str(stage))
    path = tmp_path / 'mcd.toml'; configuration(path, cache_dir=cfg_cache.custom_cache_dir, ref=ref, include_ref=True)
    path.write_text(path.read_text().replace('name="large"', 'name="custom"'))
    cfg = config.load_config(str(path), allow_recover_from_mcc=False)
    cfg, _, _, _ = config.apply_runtime_overrides(cfg, {key: value})
    cfg, _, _, _ = config.apply_runtime_overrides(cfg, {key: None})
    assert getattr(cfg, config._RUNTIME_TO_ATTR[key]) is None
    config.upsert_runtime_values(str(path), {key: None})
    restored = config.load_config(str(path), allow_recover_from_mcc=False)
    assert getattr(restored, config._RUNTIME_TO_ATTR[key]) is None
    assert config.runtime_overrides_from_config_file(str(path))[key] is None
    assert config.runtime_effective_map(restored)[key] is None


def test_literal_null_selection_is_distinct_from_absent_tombstone_and_roundtrips(tmp_path, monkeypatch):
    from test_universal_workload import configuration
    cache_cfg, ref, _, stage = runtime_resource_fixture(tmp_path)
    monkeypatch.setattr(config, 'DEFAULT_RUNTIME_ARTIFACT_DIR', str(stage))
    path = tmp_path / 'mcd.toml'
    configuration(path, cache_dir=cache_cfg.custom_cache_dir, ref=ref, include_ref=True)
    path.write_text(path.read_text().replace('name="large"', 'name="custom"'))
    key = 'universal_workload_resource_ref'
    loaded = config.load_config(str(path), allow_recover_from_mcc=False)
    cleared, _, _, _ = config.apply_runtime_overrides(loaded, {key: None})
    assert cleared.universal_workload_resource_ref is None
    assert config.runtime_effective_map(cleared)[key] is None
    daemon._persist_resolved_runtime_to_config(loaded, {key: ref}, {key: None}, {}, full_snapshot=True)
    loaded = config.load_config(str(path), allow_recover_from_mcc=False)
    assert loaded.universal_workload_resource_ref is None  # No initial-package resurrection.
    assert local_runtime_overrides(loaded)[key] is None
    assert config.runtime_effective_map(loaded)[key] is None
    assert not daemon._runtime_snapshot_loss_detected(local_runtime_overrides(loaded), {key: None})
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    daemon._persist_resolved_runtime_to_config(loaded, {key: None}, {key: None}, {}, full_snapshot=True)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
