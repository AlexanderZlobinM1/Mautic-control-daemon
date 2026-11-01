"""Explicit empty Dev never selects an Approved/latest package implicitly."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcd_agent import config, self_update


@pytest.mark.parametrize('policy,channel,expected', [('dev','approved','dev'), ('','dev','dev'), ('','Dev','dev'),
                                                   ('unknown','dev','dev'), ('approved','dev','approved'),
                                                   ('','approved','approved'), ('','test','test'), ('','cluster','cluster')])
def test_dev_is_an_explicit_policy_with_existing_precedence(policy, channel, expected):
    assert self_update._update_policy(SimpleNamespace(mcd_update_policy=policy,mcd_update_channel=channel)) == expected


@pytest.mark.parametrize('runtime', ['mcd_update_policy="dev"', 'mcd_update_channel="Dev"',
                                   'mcd_update_policy="unknown"\nmcd_update_channel="dev"'])
def test_config_normalization_keeps_dev_instead_of_approved(tmp_path, runtime):
    path=tmp_path/'mcd.toml';path.write_text('[profile]\nname="custom"\n[runtime]\n'+runtime+'\n')
    cfg=config.load_config(str(path),allow_recover_from_mcc=False)
    if 'unknown' not in runtime:
        assert cfg.mcd_update_policy.lower()=='dev'
    assert self_update._update_policy(cfg)=='dev'


def test_empty_dev_response_has_no_package_selection_or_download(tmp_path, monkeypatch):
    path=tmp_path/'mcd.toml';path.write_text('[profile]\nname="custom"\n[runtime]\nmcd_update_policy="dev"\n'
                                          'mcd_auto_update_enabled=true\nbackup_enabled=false\n'
                                          '[mcc]\nurl="https://fixture.invalid"\ntoken="synthetic-unit-fixture"\n')
    cfg=config.load_config(str(path),allow_recover_from_mcc=False)
    response={'policy':'dev','reason':'no_build_for_policy','status':'up_to_date'}
    sent=[];states=[]
    def post(url,payload,token,**kwargs):
        assert payload['update_policy']=='dev';sent.append(payload);return dict(response)
    monkeypatch.setattr(self_update,'_post_json',post)
    monkeypatch.setattr(self_update,'_read_state',lambda cfg:{})
    monkeypatch.setattr(self_update,'_write_state',lambda cfg,value:states.append(value))
    monkeypatch.setattr(self_update,'_maybe_run_update_cleanup',lambda *a,**kw:False)
    monkeypatch.setattr(self_update,'recover_stale_update_admission_barrier',lambda *a,**kw:{})
    monkeypatch.setattr(self_update,'_active_mautic_processes',lambda:[])
    monkeypatch.setattr(self_update,'installed_agent_version',lambda:self_update.__version__)
    monkeypatch.setattr(self_update,'_agent_package_sync_needed',lambda version:False)
    monkeypatch.setattr(self_update,'apply_update',lambda *a,**kw:pytest.fail('empty Dev must not apply any package'))
    monkeypatch.setattr(self_update,'_download_package',lambda *a,**kw:pytest.fail('empty Dev must not download Approved/latest'))
    message,retry=self_update.maybe_auto_update(cfg,force=True)
    assert message is None and retry>0 and len(sent)==1
    assert states[-1]['last_decision']==response
    assert not states[-1]['last_decision'].get('package_url')
