import sys
import tomllib
from types import SimpleNamespace

import pytest
from mcd_agent.hardware_profile import reconcile_hardware_profile, write_profile_selection


@pytest.mark.parametrize('hardware,mode', [('mini','auto'),('farm','manual')])
def test_hardware_reconciliation_preserves_workload_profile_and_overrides(tmp_path, hardware, mode):
    config=tmp_path/'mcd.toml'
    config.write_text(f'[profile]\nname="hiload"\n[hardware]\nprofile="{hardware}"\n[runtime]\ncommand_timeout_sec=1234\nsegment_priority_parallel_idle=19\nimport_stall_timeout_sec=987\n')
    before=tomllib.loads(config.read_text())
    install=tmp_path/'state'
    write_profile_selection(str(install),mode=mode,profile=hardware,cpu_count=2,memory_kib=0)
    result=reconcile_hardware_profile(config_path=str(config),install_dir=str(install),cpu_count=4,memory_kib=8*1024*1024)
    after=tomllib.loads(config.read_text())
    assert result.changed
    assert before['profile']==after['profile']
    assert before['runtime']==after['runtime']
    assert before['hardware']!=after['hardware']


@pytest.mark.parametrize('command', ['run','run-once'])
def test_startup_hardware_change_does_not_reconcile_workload_cron_or_emit_workload_profile_event(monkeypatch, command):
    from mcd_agent import cli
    cfg=SimpleNamespace(profile_name='hiload')
    monkeypatch.setattr(sys,'argv',['mcd-cli',command,'--config','/unused/config'])
    monkeypatch.setattr(cli,'reconcile_hardware_profile',lambda **kw: SimpleNamespace(changed=True,previous_profile='mini',profile='midi'))
    monkeypatch.setattr(cli,'load_config',lambda *a,**kw: cfg)
    monkeypatch.setattr(cli,'maybe_notify_update',lambda cfg: None)
    monkeypatch.setattr(cli,'reconcile_managed_cron',lambda **kw: pytest.fail('hardware selection must not change workload cron'))
    monkeypatch.setattr(cli,'queue_profile_event',lambda *a,**kw: pytest.fail('hardware selection must not emit workload profile event'))
    calls=[]
    monkeypatch.setattr(cli,'run_loop',lambda config,**kw: calls.append((config,kw)))
    assert cli.main()==0
    assert calls[0][0].profile_name=='hiload'


def test_explicit_workload_profile_selection_does_not_change_hardware_selection_state(monkeypatch):
    from mcd_agent import cli
    cfg=SimpleNamespace(profile_name='mini',scheduler_pause_flag_path='/unused/pause')
    monkeypatch.setattr(sys,'argv',['mcd-cli','profile','maxi','--yes','--config','/unused/config'])
    monkeypatch.setattr(cli,'load_config',lambda *a,**kw: cfg)
    monkeypatch.setattr(cli,'maybe_notify_update',lambda cfg: None)
    monkeypatch.setattr(cli,'write_profile_selection',lambda *a,**kw: pytest.fail('workload selection must not change hardware marker'))
    monkeypatch.setattr(cli,'profile_set',lambda **kw: SimpleNamespace(ok=True,lines=[]))
    monkeypatch.setattr(cli,'queue_profile_event',lambda *a,**kw: None)
    monkeypatch.setattr(cli,'_push_state_after_change',lambda *a: None)
    assert cli.main()==0


def test_hardware_auto_uses_hardware_marker_and_never_host_profile_or_cron(monkeypatch):
    from mcd_agent import cli
    cfg=SimpleNamespace(profile_name='hiload',hardware_profile_name='mini')
    monkeypatch.setattr(sys,'argv',['mcd-cli','profile','auto','--config','/unused/config'])
    monkeypatch.setattr(cli,'load_config',lambda *a,**kw: cfg)
    monkeypatch.setattr(cli,'maybe_notify_update',lambda cfg: None)
    markers=[]
    monkeypatch.setattr(cli,'write_profile_selection',lambda *a,**kw: markers.append(kw))
    monkeypatch.setattr(cli,'reconcile_hardware_profile',lambda **kw: SimpleNamespace(changed=True,previous_profile='mini',profile='midi',cpu_count=4,memory_kib=8*1024*1024))
    monkeypatch.setattr(cli,'reconcile_managed_cron',lambda **kw: pytest.fail('hardware auto must not change workload cron'))
    monkeypatch.setattr(cli,'queue_profile_event',lambda *a,**kw: pytest.fail('hardware auto must not emit workload profile event'))
    monkeypatch.setattr(cli.subprocess,'run',lambda *a,**kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(cli,'_push_state_after_change',lambda *a: None)
    assert cli.main()==0
    assert markers==[{'mode':'auto','profile':'mini'}]
    assert cfg.profile_name=='hiload'
