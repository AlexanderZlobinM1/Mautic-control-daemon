import pytest
from types import SimpleNamespace

pytestmark = pytest.mark.usefixtures("installed_workload_data")

from mcd_agent import daemon
from mcd_agent.config import (
    apply_runtime_overrides, load_config, upsert_section_values,
    runtime_overrides_from_config_file, upsert_runtime_values,
)


def test_blank_required_values_preserve_split_baseline_and_other_settings(tmp_path):
    root = tmp_path / 'mcd.toml'
    local = tmp_path / 'mcd.local.toml'
    root.write_text('[include]\nfiles=["mcd.local.toml"]\n[profile]\nname="custom"\n'
                    '[runtime]\nimport_stall_timeout_sec=2400\nsegment_lazy_update_enabled=true\n')
    local.write_text('[runtime]\nimport_stall_timeout_sec=""\nsegment_lazy_update_enabled=""\n'
                     'campaign_super_priority=[78,82]\nsegment_super_priority=[93,94]\n')
    before = local.read_bytes()
    cfg = load_config(str(root), allow_recover_from_mcc=False)
    assert cfg.import_stall_timeout_sec == 2400
    assert cfg.segment_lazy_update_enabled is True
    assert cfg.campaign_super_priority == [78, 82]
    assert cfg.segment_super_priority == [93, 94]
    assert local.read_bytes() == before
    wire = runtime_overrides_from_config_file(str(root))
    assert wire['import_stall_timeout_sec'] == 2400
    assert wire['segment_lazy_update_enabled'] is True


@pytest.mark.parametrize('value', [None, '', '   '])
@pytest.mark.parametrize('key', ['import_stall_timeout_sec', 'segment_lazy_update_enabled',
                                 'monitored_email_parser_instance_settings'])
def test_new_null_required_setting_is_rejected_before_apply_or_persistence(tmp_path, key, value):
    root = tmp_path / 'mcd.toml'
    root.write_text('[profile]\nname="custom"\n')
    cfg = load_config(str(root), allow_recover_from_mcc=False)
    before = root.read_bytes()
    merged, applied, unsupported, blocked = apply_runtime_overrides(cfg, {key: value})
    assert merged == cfg
    assert applied == []
    assert unsupported == []
    assert blocked == [key]
    for write in (
        lambda: upsert_runtime_values(str(root), {key: value}),
        lambda: upsert_section_values(str(root), 'runtime', {key: value}),
    ):
        with pytest.raises(ValueError, match=key):
            write()
        assert root.read_bytes() == before


def test_invalid_remote_fields_do_not_block_valid_remote_fields(tmp_path):
    root = tmp_path / 'mcd.toml'
    root.write_text('[profile]\nname="custom"\n')
    cfg = load_config(str(root), allow_recover_from_mcc=False)
    merged, applied, unsupported, blocked = apply_runtime_overrides(
        cfg,
        {
            'import_stall_timeout_sec': '',
            'segment_lazy_update_enabled': '',
            'monitored_email_parser_instance_settings': '',
            'campaign_super_priority': [78, 82],
            'segment_super_priority': [93, 94],
        },
    )
    assert merged.campaign_super_priority == [78, 82]
    assert merged.segment_super_priority == [93, 94]
    assert applied == ['campaign_super_priority', 'segment_super_priority']
    assert unsupported == []
    assert blocked == [
        'import_stall_timeout_sec',
        'monitored_email_parser_instance_settings',
        'segment_lazy_update_enabled',
    ]


def test_resolved_persistence_skips_invalid_fields_and_materializes_valid_settings(tmp_path):
    root = tmp_path / 'mcd.toml'
    root.write_text('[profile]\nname="custom"\n[runtime]\nimport_stall_timeout_sec=""\n'
                    'campaign_super_priority=[82]\nsegment_super_priority=[93,94]\n')
    cfg = load_config(str(root), allow_recover_from_mcc=False)
    daemon._persist_resolved_runtime_to_config(
        SimpleNamespace(config_file_path=str(root), profile_name='custom'),
        {'campaign_super_priority': [82]},
        {
            'import_stall_timeout_sec': '',
            'segment_lazy_update_enabled': '',
            'monitored_email_parser_instance_settings': '',
            'campaign_super_priority': [78, 82],
            'segment_super_priority': [93, 94],
        },
        {},
        full_snapshot=True,
    )
    restored = load_config(str(root), allow_recover_from_mcc=False)
    assert restored.campaign_super_priority == [78, 82]
    assert restored.segment_super_priority == [93, 94]
    assert restored.import_stall_timeout_sec == 1800


def test_update_recovery_drops_invalid_fields_but_restores_valid_settings(tmp_path):
    root = tmp_path / 'mcd.toml'
    root.write_text('[profile]\nname="custom"\n[runtime]\ncampaign_super_priority=[82]\n')
    cfg = load_config(str(root), allow_recover_from_mcc=False)
    recovered = daemon._persist_recovered_runtime_values(
        cfg,
        {'campaign_super_priority': [82]},
        {
            'import_stall_timeout_sec': '',
            'campaign_super_priority': [78, 82],
            'segment_super_priority': [93, 94],
        },
    )
    assert recovered == {
        'campaign_super_priority': [78, 82],
        'segment_super_priority': [93, 94],
    }
    restored = load_config(str(root), allow_recover_from_mcc=False)
    assert restored.campaign_super_priority == [78, 82]
    assert restored.segment_super_priority == [93, 94]


def test_real_zero_false_empty_containers_and_optional_null_survive_restart(tmp_path):
    root = tmp_path / 'mcd.toml'
    root.write_text('[profile]\nname="custom"\n')
    updates = {'campaign_limit': 0, 'segment_lazy_update_enabled': False,
               'campaign_super_priority': [], 'monitored_email_parser_instance_settings': {},
               'mautic6_core_patch_version_min': None}
    upsert_runtime_values(str(root), updates)
    cfg = load_config(str(root), allow_recover_from_mcc=False)
    assert cfg.campaign_limit == 0
    assert cfg.segment_lazy_update_enabled is False
    assert cfg.campaign_super_priority == []
    assert cfg.monitored_email_parser_instance_settings == {}
    assert cfg.mautic6_core_patch_version_min is None
    assert runtime_overrides_from_config_file(str(root)) == updates


def test_blank_without_lower_value_is_not_advertised_as_local_change(tmp_path):
    root = tmp_path / 'mcd.toml'
    root.write_text('[profile]\nname="custom"\n[runtime]\nimport_stall_timeout_sec=""\n'
                    'segment_lazy_update_enabled=""\nmonitored_email_parser_instance_settings=""\n')
    cfg = load_config(str(root), allow_recover_from_mcc=False)
    assert cfg.import_stall_timeout_sec == 1800
    assert runtime_overrides_from_config_file(str(root)) == {}
    # The ordinary desired-state persistence can replace all three broken
    # values without a config reset or a separate recovery writer.
    restored = {'import_stall_timeout_sec': 2700, 'segment_lazy_update_enabled': True,
                'monitored_email_parser_instance_settings': {'instance-1': {'enabled': True}}}
    upsert_runtime_values(str(root), restored)
    restarted = load_config(str(root), allow_recover_from_mcc=False)
    assert restarted.import_stall_timeout_sec == 2700
    assert restarted.segment_lazy_update_enabled is True
    assert restarted.monitored_email_parser_instance_settings == restored['monitored_email_parser_instance_settings']
    assert runtime_overrides_from_config_file(str(root)) == restored

