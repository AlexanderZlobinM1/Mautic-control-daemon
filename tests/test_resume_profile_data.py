"""Resume fallback is catalog policy; saved and legacy workflow stay core."""
from pathlib import Path

import pytest

from mcd_agent import config, mode, custom_scripts
from mcd_agent.custom_scripts import CustomSelectedTaskError
from mcd_agent.universal_workload import required_workload_resource, workload_resume_profile


@pytest.mark.parametrize('saved', [None, '', '  \n\t'])
def test_missing_saved_profile_resumes_exact_data_default_offline(resume_workload_data, tmp_path, monkeypatch, saved):
    path = Path(resume_workload_data['config_path']); path.write_text('[profile]\nname="passive"\n')
    install = tmp_path / 'install'
    if saved is not None:
        previous = mode._profile_file(str(install)); previous.parent.mkdir(parents=True); previous.write_text(saved)
    before_config = (path.read_bytes(), path.stat().st_mtime_ns)
    cache = resume_workload_data['cache']; before_cache = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                                                         for p in cache.rglob('*') if p.is_file()}
    monkeypatch.setattr(custom_scripts.urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('matching selected DATA must be offline'))
    assert mode._activation_profile(str(path), str(install), 'passive') == 'tiny'
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before_config
    assert all((p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) == state for p, state in before_cache.items())


@pytest.mark.parametrize('saved,expected', [(' HiLoad\n', 'hiload'), (' FARM\n', 'farm'), ('Custom', 'custom')])
def test_saved_name_precedes_default_even_with_old_catalog(installed_workload_data, tmp_path, monkeypatch, saved, expected):
    install = tmp_path / 'install'; previous = mode._profile_file(str(install))
    previous.parent.mkdir(parents=True); previous.write_text(saved)
    monkeypatch.setattr(config, 'load_config', lambda *a, **kw: pytest.fail('saved name must not consult fallback DATA'))
    assert mode._activation_profile(installed_workload_data['config_path'], str(install), 'passive') == expected


@pytest.mark.parametrize('current', ['custom', 'hiload', 'farm'])
def test_already_active_or_custom_profile_is_not_replaced_by_resume_default(tmp_path, monkeypatch, current):
    monkeypatch.setattr(config, 'load_config', lambda *a, **kw: pytest.fail('active name must not consult passive fallback'))
    assert mode._activation_profile(str(tmp_path / 'unused.toml'), str(tmp_path), current) == current


def test_old_data_without_default_fails_before_config_or_cron_mutation(installed_workload_data, tmp_path, monkeypatch):
    path = Path(installed_workload_data['config_path']); path.write_text('[profile]\nname="passive"\n')
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    monkeypatch.setattr(mode.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(mode, '_write_profile_name', lambda *a, **kw: pytest.fail('invalid policy must not mutate config'))
    monkeypatch.setattr(mode, '_read_crontab', lambda *a, **kw: pytest.fail('invalid policy must not reach cron'))
    with pytest.raises(CustomSelectedTaskError) as error:
        mode.mode_activate(config_path=str(path), install_dir=str(tmp_path / 'install'), pause_flag_path=str(tmp_path / 'pause'))
    assert error.value.code == 'custom_selected_task_resume_profile_default_missing_or_invalid'
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize('name', [None, 1, 'unknown', 'farm'])
def test_catalog_default_must_reference_a_valid_selectable_profile(resume_workload_data, name):
    loaded = config.load_config(resume_workload_data['config_path'], allow_recover_from_mcc=False)
    data = required_workload_resource(loaded)
    data['catalog']['defaults'] = {'resume_profile': name}
    with pytest.raises(CustomSelectedTaskError):
        workload_resume_profile(data)
