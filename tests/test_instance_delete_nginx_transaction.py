from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mcd_agent import instance_delete as delete, nginx_baseline as baseline


@pytest.fixture
def layout(tmp_path, monkeypatch):
    dirs = [tmp_path / name for name in ('sites-enabled', 'sites-available', 'conf.d')]
    for path in dirs:
        path.mkdir()
    monkeypatch.setattr(delete, '_NGINX_DIRS', tuple(dirs))
    monkeypatch.setattr(baseline, 'DEFAULT_DENY_FILE', dirs[2] / '00-mcd-default-deny.conf')
    return dirs


def plan(paths=()):
    return delete.InstanceDeletePlan(Path('/var/www/apetit/public_html'), None, ['s.apetit.rs'], list(paths), '', '', '', '', '', False, True, False)


def result():
    return {'deleted': {'vhost': []}, 'warnings': []}


def test_inventory_conf_d_and_aliases_precedes_target_unlink(layout):
    enabled, available, conf_d = layout
    site = conf_d / 'unusual.conf'
    site.write_text('server { server_name "s.apetit.rs"; }')
    (enabled / 'alias').symlink_to(site)
    (available / 's.apetit.rs.conf').symlink_to(site)
    found = delete._nginx_candidates(Path('/var/www/apetit/public_html'), ['s.apetit.rs'])
    assert set(found) == {site, enabled / 'alias', available / 's.apetit.rs.conf'}
    assert found[-1] == site
    for path in found:
        delete._disable_nginx_vhost(path)
    assert delete._nginx_candidates(Path('/var/www/apetit/public_html'), ['s.apetit.rs']) == []


def test_broken_alias_is_owned_by_exact_missing_target_filename(layout):
    enabled, available, _ = layout
    link = enabled / 'legacy-alias'
    link.symlink_to(available / 's.apetit.rs.conf')
    assert delete._nginx_candidates(Path('/var/www/apetit/public_html'), ['s.apetit.rs']) == [link]


def test_shared_vhost_is_rejected_without_mutation(layout):
    enabled, available, _ = layout
    shared = available / 's.apetit.rs.conf'
    content = 'server { server_name s.apetit.rs keep.example.com; }'
    shared.write_text(content)
    (enabled / shared.name).symlink_to(shared)
    with pytest.raises(RuntimeError, match='shared nginx config'):
        delete._remove_nginx_configs_safely(plan(), result())
    assert shared.read_text() == content
    assert (enabled / shared.name).is_symlink()


def test_nginx_ownership_ignores_comments_and_map_entries_but_keeps_all_server_names():
    text = 'map $x $y { server_name delete.example.invalid; }\nserver { server_name keep.example.invalid; server_name alias.example.invalid; } # server_name delete.example.invalid;'
    assert delete._nginx_server_names(text) == {'keep.example.invalid', 'alias.example.invalid'}


@pytest.mark.parametrize('failure', ['test', 'reload', 'remaining_domain'])
def test_failure_restores_all_scoped_files_and_aliases(layout, failure):
    enabled, available, _ = layout
    site = available / 's.apetit.rs.conf'
    site.write_text('server { server_name s.apetit.rs; }')
    alias = enabled / 'alias'
    alias.symlink_to(site)
    keep = available / 'keep.conf'
    keep.write_text('server { server_name keep.example.com; }')
    def run(args, **kwargs):
        if args == ['nginx', '-t'] and failure == 'test':
            return 1, 'fixture validation failed'
        if args == ['systemctl', 'reload', 'nginx'] and failure == 'reload':
            return 1, 'fixture reload failed'
        if args == ['nginx', '-T'] and failure == 'remaining_domain':
            return 0, 'server { server_name s.apetit.rs; }'
        return 0, ''
    with patch.object(delete, '_run', side_effect=run), pytest.raises(RuntimeError):
        delete._remove_nginx_configs_safely(plan(), result())
    assert alias.is_symlink() and alias.resolve() == site
    assert site.read_text() == 'server { server_name s.apetit.rs; }'
    assert keep.read_text() == 'server { server_name keep.example.com; }'


def test_delete_never_repairs_or_changes_shared_default(layout):
    deny = baseline.DEFAULT_DENY_FILE
    original = baseline.DEFAULT_DENY_MARKER + '\nserver { server_name _; ssl_certificate /etc/letsencrypt/live/s.apetit.rs/fullchain.pem; }'
    deny.write_text(original)
    site = layout[1] / 's.apetit.rs.conf'
    site.write_text('server { server_name s.apetit.rs; }')
    with patch.object(delete, '_run', return_value=(1, 'broken shared default')), patch.object(baseline, '_ensure_default_deny_config') as repair, pytest.raises(RuntimeError):
        delete._remove_nginx_configs_safely(plan(), result())
    assert deny.read_text() == original
    assert site.read_text() == 'server { server_name s.apetit.rs; }'
    repair.assert_not_called()


def test_other_vhost_certificate_reference_is_retained(layout):
    site = layout[1] / 's.apetit.rs.conf'
    site.write_text('server { server_name s.apetit.rs; }')
    dump = 'server { server_name keep.example.com; ssl_certificate /etc/letsencrypt/live/s.apetit.rs/fullchain.pem; }'
    with patch.object(delete, '_run', side_effect=lambda args, **kwargs: (0, dump if args == ['nginx', '-T'] else '')):
        assert delete._remove_nginx_configs_safely(plan(), result()) == {'s.apetit.rs'}


def test_validation_failure_precedes_all_irreversible_delete_actions(layout):
    site = layout[1] / 's.apetit.rs.conf'
    site.write_text('server { server_name s.apetit.rs; }')
    target = plan([site])
    target.delete_db = target.delete_files = True
    with patch.object(delete, 'build_delete_plan', return_value=target), patch.object(delete.os, 'geteuid', return_value=0), patch.object(delete, '_run', return_value=(1, 'bad nginx')), patch.object(delete, '_mysql_exec') as db, patch.object(delete, '_remove_dedicated_certificate') as cert, patch.object(delete, '_remove_instance_root') as files:
        with pytest.raises(RuntimeError):
            delete.delete_instance_artifacts(SimpleNamespace(), root=str(target.root), delete_files=True, delete_db=True, delete_vhost=True, yes=True)
    db.assert_not_called()
    cert.assert_not_called()
    files.assert_not_called()


def test_default_stub_has_no_certificates_domains_or_generation(layout):
    with patch.object(baseline, '_active_default_server_ports', return_value=set()), patch.object(baseline.subprocess, 'run') as process:
        baseline._ensure_default_deny_config(layout[2] / 'backups', {})
    content = baseline.DEFAULT_DENY_FILE.read_text()
    assert 'ssl_reject_handshake on;' in content
    assert 'ssl_certificate' not in content
    assert '/letsencrypt/' not in content
    assert delete._nginx_server_names(content) == {'_'}
    process.assert_not_called()
