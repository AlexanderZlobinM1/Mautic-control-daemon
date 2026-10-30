import json
from dataclasses import replace
from unittest.mock import patch

from mcd_agent import backup
from mcd_agent.config import load_config


def config(tmp_path):
    path = tmp_path / "mcd.toml"
    path.write_text('[profile]\nname="custom"\n[runtime]\nstate_db_path="' + str(tmp_path / 'state.db') + '"\n')
    return replace(load_config(str(path), allow_recover_from_mcc=False), backup_secrets_key_path=str(tmp_path / 'key'), backup_storage_kind='local', backup_local_path=str(tmp_path / 'storage'), backup_local_require_mount=False)


def envelope(profile, updated=10):
    return {'schema': 'mcd-backup-profile-v1', 'version': 1, 'profile': profile, 'profile_updated_at': updated, 'profile_sha256': backup.backup_profile_sync_hash(profile)}


def test_mcc_published_v1_fixture_and_transport_hash_are_distinct():
    raw = {'settings': {'backup_enabled': True, 'backup_schedule_enabled': True, 'backup_schedule_interval_sec': 86400, 'backup_schedule_pre_pause_sec': 3600, 'backup_schedule_quiet_hour': 2, 'backup_schedule_quiet_minute': 7, 'backup_schedule_quiet_window_min': 60}}
    canonical = backup.canonical_backup_profile(raw)
    assert backup.backup_profile_sync_hash(canonical) == '04bac0b5376356121a0a61b9552ed38d1d9ffb949ba20971b5c75c11b7d068bc'
    assert backup.backup_profile_sync_hash(raw) != backup.backup_profile_sync_hash(canonical)


def test_partial_v1_profile_persists_remote_root_auth_and_replays_without_default_injection(tmp_path):
    cfg = config(tmp_path)
    profile = {'enabled': True, 'remote_root_dir': 'deleted-instances', 'storage': {'kind': 'local', 'local_path': str(tmp_path / 'storage'), 'require_mount': False, 'password': 'fixture-only-secret'}}
    with patch.object(backup, '_validate_cfg'):
        candidate, receipt = backup.backup_profile_apply_sync(cfg, envelope(profile))
        assert receipt['status'] == 'ok'
        assert receipt['effective_sha256'] == backup.backup_profile_sync_hash(profile)
        assert set(receipt['effective_profile']) == set(profile)
        assert receipt['effective_profile']['remote_root_dir'] == 'deleted-instances'
        assert 'fixture-only-secret' not in json.dumps(receipt)
        assert backup.backup_profile_get(candidate)['storage']['password'] == 'fixture-only-secret'
        assert backup.backup_profile_for_push(candidate) == profile
        assert backup.backup_profile_apply_sync(candidate, envelope(profile))[1]['status'] == 'ok'


def test_mapped_schedule_is_effective_and_hashed_before_receipt_masking(tmp_path):
    cfg = config(tmp_path)
    profile = {'settings': {'backup_enabled': True, 'backup_schedule_enabled': True, 'backup_schedule_quiet_minute': 7, 'backup_schedule_interval_sec': 86400}}
    with patch.object(backup, '_validate_cfg'):
        candidate, receipt = backup.backup_profile_apply_sync(cfg, envelope(profile))
    assert receipt['status'] == 'ok'
    assert candidate.backup_schedule_quiet_minute == 7
    assert candidate.backup_schedule_enabled is True
    assert receipt['effective_profile'] == backup.canonical_backup_profile(profile)
    assert receipt['profile_sha256'] == backup.backup_profile_sync_hash(profile)
    assert receipt['effective_sha256'] == backup.backup_profile_sync_hash(backup.canonical_backup_profile(profile))
    assert backup.backup_profile_for_push(candidate) == backup.canonical_backup_profile(profile)


def test_unknown_or_unapplied_field_is_rejected_before_any_write(tmp_path):
    cfg = config(tmp_path)
    for profile in ({'enabled': True, 'unknown': 'never-echo-this'}, {'enabled': True, 'storage': {'password': 'new-fixture-secret'}}):
        candidate = replace(cfg, backup_ssh_password='old-fixture-secret')
        with patch.object(backup, '_cfg_with_profile_payload', return_value=candidate), patch.object(backup, '_validate_cfg'), patch.object(backup, 'backup_profile_set') as write:
            _, receipt = backup.backup_profile_apply_sync(candidate, envelope(profile))
        assert receipt['status'] in {'error', 'unsynced'}
        assert 'never-echo-this' not in json.dumps(receipt)
        write.assert_not_called()


def test_newer_request_does_not_reapply_older_db_values_to_candidate(tmp_path):
    cfg = config(tmp_path)
    with patch.object(backup, '_validate_cfg'):
        cfg, old = backup.backup_profile_apply_sync(cfg, envelope({'enabled': True, 'remote_root_dir': 'old'}, 10))
        cfg, new = backup.backup_profile_apply_sync(cfg, envelope({'enabled': True, 'remote_root_dir': 'new'}, 11))
    assert old['status'] == new['status'] == 'ok'
    assert new['effective_profile']['remote_root_dir'] == 'new'


def test_disabled_full_profile_keeps_settings_and_can_replay(tmp_path):
    cfg = config(tmp_path)
    profile = {'enabled': False, 'remote_root_dir': 'backup', 'source': 'config', 'storage': {'kind': 'local'}}
    cfg, receipt = backup.backup_profile_apply_sync(cfg, envelope(profile))
    replay = backup.backup_profile_apply_sync(cfg, envelope(profile))[1]
    assert receipt['status'] == replay['status'] == 'ok'
    assert receipt['effective_profile'] == profile
    assert backup.backup_profile_for_push(cfg) == profile


def test_invalid_epoch_never_writes_profile(tmp_path):
    cfg = config(tmp_path)
    for timestamp in (float('nan'), float('inf'), 0, -1):
        with patch.object(backup, 'backup_profile_set') as write:
            receipt = backup.backup_profile_apply_sync(cfg, envelope({'enabled': True}, timestamp))[1]
        assert receipt['reason'] == 'invalid_updated_at'
        write.assert_not_called()


def test_explicit_empty_auth_is_applied_while_omitted_fields_are_preserved(tmp_path):
    cfg = replace(config(tmp_path), backup_ssh_password='old-fixture', backup_ssh_key_file='/fixture/old-key', backup_mysql_password='old-db-fixture')
    profile = {'enabled': True, 'storage': {'password': '', 'key_file': ''}, 'mysql': {'password': ''}}
    with patch.object(backup, '_validate_cfg'):
        cfg, receipt = backup.backup_profile_apply_sync(cfg, envelope(profile))
    assert receipt['status'] == 'ok'
    assert cfg.backup_ssh_password == cfg.backup_ssh_key_file == cfg.backup_mysql_password == ''
    assert cfg.backup_local_path == str(tmp_path / 'storage')
