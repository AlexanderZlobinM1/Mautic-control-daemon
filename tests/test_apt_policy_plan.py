import pytest

from mcd_agent import apt_profile


@pytest.mark.parametrize('legacy_value', [True, 'true', 'yes', 1])
@pytest.mark.parametrize('dry_run', [True, False])
def test_obsolete_node20_policy_fails_before_probes_or_mutations(monkeypatch, legacy_value, dry_run):
    def forbidden(*args, **kwargs):
        raise AssertionError('no host work is allowed for obsolete policy')

    monkeypatch.setattr(apt_profile, 'collect_apt_state', forbidden)
    monkeypatch.setattr(apt_profile, '_run', forbidden)
    monkeypatch.setattr(apt_profile, '_apply_repo_profiles', forbidden)
    result = apt_profile.apply_apt_profile({'ensure_nodejs20': legacy_value}, dry_run=dry_run)
    assert result['status'] == 'error'
    assert result['reason'] == 'apt_profile_legacy_node20_unsupported'
    assert result['actions'] == result['changed_files'] == []


@pytest.mark.parametrize('legacy', [{}, {'ensure_nodejs20': False}])
def test_external_baseline_plan_has_only_enabled_optional_actions(monkeypatch, legacy):
    probes = []
    monkeypatch.setattr(apt_profile, 'collect_apt_state', lambda **kwargs: probes.append(kwargs) or {})
    profile = {**legacy, 'packages_present': ['nginx', 'php8.4-fpm'], 'ensure_composer_global': True,
               'ensure_var_www': False, 'zabbix_agent_enabled': False, 'zabbix_mysql_monitor_enabled': False,
               'remove_third_party_sources': False, 'refresh_cache': False}
    result = apt_profile.apply_apt_profile(profile, dry_run=True)
    assert result['status'] == 'planned'
    assert result['packages_present'] == profile['packages_present']
    assert 'optional_package_ops' in result['actions']
    assert 'ensure_package_services_started' in result['actions']
    assert 'ensure_composer_global' in result['actions']
    assert not {'ensure_nodejs20', 'ensure_var_www', 'zabbix_agent_config', 'zabbix_mysql_monitor_bootstrap',
                'remove_third_party_sources', 'apt_update'} & set(result['actions'])
    assert probes[0]['auto_bootstrap_zabbix'] is False


def test_disabled_package_service_policy_does_not_plan_service_start(monkeypatch):
    monkeypatch.setattr(apt_profile, 'collect_apt_state', lambda **kwargs: {})
    result = apt_profile.apply_apt_profile({'packages_present': ['nginx'], 'ensure_package_services_started': False}, dry_run=True)
    assert 'optional_package_ops' in result['actions']
    assert 'ensure_package_services_started' not in result['actions']
