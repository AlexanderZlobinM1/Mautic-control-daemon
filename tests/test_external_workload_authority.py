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
