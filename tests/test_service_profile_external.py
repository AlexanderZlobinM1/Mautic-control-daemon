import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from mcd_agent import service_profile_external as ext, service_profiles as profiles
from mcd_agent.commercial_portal import canonical_json


@pytest.fixture
def fetched():
    profile={'pool':'qa','pm_max_children':11}
    digest='a'*64
    command={'argv':['custom','--no-detach','--expected-sha256',digest,'test-profile','--','--composition','profiles/task.json','--selection','operator-choice']}
    return {'status':'ok','component':'php_fpm','resolved_plan_key':'operator-choice','profile':profile,
      'external_handler':{'schema':'mcc-service-profile-external-handler-v1','transport':'mcd-custom-v1',
        'component':'php_fpm','resolved_plan_key':'operator-choice','state':'active','active':True,
        'minimum_runtime':{'required_capabilities':sorted(ext.CUSTOM_RUNTIME_CAPABILITIES)},
        'profile_input':{'source':'response.profile','sha256':hashlib.sha256(canonical_json(profile)).hexdigest()},
        'artifact':{'media_type':'application/gzip','key':'test-profile','sha256':digest,'entrypoint':'recipe/main.py'},
        'command':command,'dry_run_command':{'argv':command['argv']+['--dry-run']}}}


def test_descriptor_dispatch_preserves_public_result_and_arguments(fetched, monkeypatch):
    result={'status':'applied','arbitrary_public_value':[1,'operator']}
    calls=[]
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda cfg,**kw:calls.append(kw) or (0,json.dumps(result)))
    assert ext.apply_external_service_profile(None,fetched,component='php_fpm')==result
    assert calls[0]['args']==fetched['external_handler']['command']['argv'][6:]
    assert calls[0]['expected_entrypoint']=='recipe/main.py'
    assert calls[0]['capture_result'] is True
    assert calls[0]['detach'] is False


def test_dry_run_uses_only_explicit_nonmutating_descriptor(fetched,monkeypatch):
    calls=[]
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda cfg,**kw:calls.append(kw) or (0,'{"status":"planned"}'))
    assert ext.apply_external_service_profile(None,fetched,component='php_fpm',dry_run=True)['status']=='planned'
    assert calls[0]['args'][-1]=='--dry-run'
    del fetched['external_handler']['dry_run_command']
    with pytest.raises(ValueError,match='dry_run_unavailable'):
        ext.apply_external_service_profile(None,fetched,component='php_fpm',dry_run=True)
    assert len(calls)==1


@pytest.mark.parametrize('mutation', ['profile','capabilities','component','plan','pin','command','activation','transport'])
def test_admission_failure_never_executes(fetched,monkeypatch,mutation):
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:pytest.fail('must not execute'))
    h=fetched['external_handler']
    if mutation=='profile':fetched['profile']['pm_max_children']=90
    elif mutation=='capabilities':h['minimum_runtime']['required_capabilities'].append('unknown-runtime')
    elif mutation=='component':h['component']='other'
    elif mutation=='plan':h['resolved_plan_key']='other'
    elif mutation=='pin':h['artifact']['sha256']='bad'
    elif mutation=='command':h['command']['argv'][0]='sh'
    elif mutation=='activation':h['active']=False
    elif mutation=='transport':h['transport']='shell'
    with pytest.raises(ValueError):ext.apply_external_service_profile(None,fetched,component='php_fpm')


def test_inactive_candidate_is_rejected(fetched,monkeypatch):
    h=fetched['external_handler'];h.update(state='candidate',active=False)
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:(0,'{"status":"noop"}'))
    with pytest.raises(ValueError,match='inactive'):ext.apply_external_service_profile(None,fetched,component='php_fpm')


def test_selected_external_failure_cannot_fallback(fetched,monkeypatch):
    monkeypatch.setattr(profiles,'fetch_service_profile',lambda *a,**kw:fetched)
    monkeypatch.setattr(profiles,'_cluster_db_maintenance_guard',lambda *a,**kw:None)
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:(7,'validation_failed'))
    with pytest.raises(RuntimeError,match='validation_failed'):
        profiles.service_profiles_apply_once(None)


def test_inactive_candidate_cannot_execute_without_explicit_admission(fetched,monkeypatch):
    fetched['external_handler'].update(state='candidate',active=False)
    monkeypatch.setattr(profiles,'fetch_service_profile',lambda *a,**kw:fetched)
    monkeypatch.setattr(profiles,'_cluster_db_maintenance_guard',lambda *a,**kw:None)
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:pytest.fail('inactive candidate'))
    with pytest.raises(ValueError,match='external_handler_inactive'):
        profiles.service_profiles_apply_once(None)


def test_missing_descriptor_cannot_silently_use_legacy(fetched,monkeypatch):
    del fetched['external_handler']
    monkeypatch.setattr(profiles,'fetch_service_profile',lambda *a,**kw:fetched)
    monkeypatch.setattr(profiles,'_cluster_db_maintenance_guard',lambda *a,**kw:None)
    with pytest.raises(ValueError,match='external_handler_missing'):
        profiles.service_profiles_apply_once(None)


@pytest.mark.parametrize('component',['php_fpm','mysql','docker'])
def test_existing_post_advertises_actual_capabilities_without_candidate_opt_in(monkeypatch,component):
    cfg=SimpleNamespace(mcc_url='https://controller.invalid',mcc_token='test')
    monkeypatch.setattr(profiles,'resolve_agent_identity',lambda cfg:{'effective_hostname':'disposable'})
    calls=[]
    monkeypatch.setattr(profiles,'_post_json',lambda url,payload,*a,**kw:calls.append((url,payload)) or {'status':'ok'})
    profiles.fetch_service_profile(cfg,component)
    url,payload=calls[0]
    assert url.endswith('/api/v1/agent/service-profile/'+component.replace('_','-'))
    assert set(payload['external_handler_capabilities'])==ext.CUSTOM_RUNTIME_CAPABILITIES
    assert 'external_handler_candidate_opt_in' not in payload


def test_profile_pin_uses_authority_utf8_without_ascii_escaping(fetched,monkeypatch):
    fetched['profile']['operator_note']='Профиль оператора'
    exact=json.dumps(fetched['profile'],ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    fetched['external_handler']['profile_input']['sha256']=hashlib.sha256(exact).hexdigest()
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:(0,'{"status":"noop"}'))
    assert ext.apply_external_service_profile(None,fetched,component='php_fpm')=={'status':'noop'}


@pytest.mark.parametrize('actual,declared', [(None,None),('qa-cluster','qa-cluster')])
def test_mysql_context_matches_actual_current_config(fetched,monkeypatch,actual,declared):
    fetched['component']='mysql'
    fetched['external_handler'].update(component='mysql',runtime_context={'cluster_id':declared})
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:(0,'{"status":"planned"}'))
    assert ext.apply_external_service_profile(SimpleNamespace(cluster_id=actual),fetched,component='mysql',dry_run=True)=={'status':'planned'}


@pytest.mark.parametrize('actual,context', [(None,{}),(None,None),(None,{'cluster_id':''}),
    ('qa-cluster',{'cluster_id':None}),('new-cluster',{'cluster_id':'old-cluster'}),
    (None,{'cluster_id':False}),('qa-cluster',{'cluster_id':'qa-cluster','unknown':True})])
def test_mysql_missing_null_mismatch_context_cannot_begin_execution(fetched,monkeypatch,actual,context):
    fetched['component']='mysql'
    fetched['external_handler'].update(component='mysql')
    if context is not None:fetched['external_handler']['runtime_context']=context
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:pytest.fail('context must be checked before download/execution'))
    with pytest.raises(ValueError,match='runtime_context'):
        ext.apply_external_service_profile(SimpleNamespace(cluster_id=actual),fetched,component='mysql')


def test_response_and_handler_cannot_change_requested_component(fetched,monkeypatch):
    fetched['component']='mysql';fetched['external_handler']['component']='mysql'
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:pytest.fail('wrong endpoint component'))
    with pytest.raises(ValueError,match='requested_component_mismatch'):
        ext.apply_external_service_profile(None,fetched,component='php_fpm')


def failed_terminal(result):
    return {'schema':'mcd-self-hosted-executable-output-v1','contract_version':'1.0',
            'frame_type':'result','status':'failed','exit_code':1,'completed':0,
            'total':1,'rollback':'not_attempted','error_code':'task_component_operation_failed','result':result}


@pytest.mark.parametrize('status,outer', [('partial','error'),('deferred','skipped'),('skipped','skipped'),('applied','error')])
def test_failed_terminal_preserves_raw_result_and_legacy_outcome(fetched,monkeypatch,status,outer):
    raw={'status':status,'changed_files':['/managed/config'],'dynamic':{'status':'error','attempted':['operation']}}
    monkeypatch.setattr(profiles,'fetch_service_profile',lambda *a,**kw:fetched)
    monkeypatch.setattr(profiles,'_cluster_db_maintenance_guard',lambda *a,**kw:None)
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:(1,json.dumps(failed_terminal(raw))))
    result=profiles.service_profiles_apply_once(None)
    assert result['status']==outer
    assert result['apply']==raw


@pytest.mark.parametrize('raw', [{'password':'must-never-be-forwarded'}, {'value':float('nan')}])
def test_failed_payload_guards_stay_in_force(fetched,monkeypatch,raw):
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:(1,json.dumps(failed_terminal(raw))))
    with pytest.raises(RuntimeError) as error:
        ext.apply_external_service_profile(None,fetched,component='php_fpm')
    assert not isinstance(error.value,ext.ExternalProfileOperationError)
    assert "must-never-be-forwarded" not in str(error.value)


def test_mysql_public_dispatch_uses_pinned_external_context_without_old_writer(fetched,monkeypatch):
    fetched['component']='mysql'
    fetched['external_handler'].update(component='mysql',runtime_context={'cluster_id':None})
    cfg=SimpleNamespace(cluster_id=None)
    monkeypatch.setattr(profiles,'fetch_service_profile',lambda *a,**kw:fetched)
    monkeypatch.setattr(profiles,'_cluster_db_maintenance_guard',lambda *a,**kw:None)
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:(0,'{"status":"applied","changed_files":[]}'))
    result=profiles.service_profiles_apply_once(cfg,component='mysql')
    assert result['status']=='ok' and result['apply']['status']=='applied'


def test_mysql_public_dispatch_rejects_context_drift_before_artifact_execution(fetched,monkeypatch):
    fetched['component']='mysql'
    fetched['external_handler'].update(component='mysql',runtime_context={'cluster_id':'different'})
    monkeypatch.setattr(profiles,'fetch_service_profile',lambda *a,**kw:fetched)
    monkeypatch.setattr(profiles,'_cluster_db_maintenance_guard',lambda *a,**kw:None)
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:pytest.fail('context mismatch must stop before execution'))
    with pytest.raises(ValueError,match='runtime_context_mismatch'):
        profiles.service_profiles_apply_once(SimpleNamespace(cluster_id=None),component='mysql',allow_cluster_db_maintenance=True)


@pytest.mark.parametrize('dry_run,raw,exit_code,outer', [
    (True, {'status':'planned','commands':[['operator-command']]}, 0, 'ok'),
    (False, {'status':'noop','reason':'docker_runtime_ready'}, 0, 'ok'),
    (False, {'status':'error','reason':'command_failed:apt-get update:failure'}, 1, 'error'),
])
def test_docker_public_dispatch_preserves_external_result_without_builtin_fallback(fetched,monkeypatch,dry_run,raw,exit_code,outer):
    fetched.update(component='docker',resolved_plan_key='universal',profile={})
    fetched['external_handler'].update(component='docker',resolved_plan_key='universal',
        profile_input={'source':'response.profile','sha256':hashlib.sha256(b'{}').hexdigest()})
    monkeypatch.setattr(profiles,'fetch_service_profile',lambda *a,**kw:fetched)
    assert not hasattr(profiles,'apply_docker_profile')
    calls=[]
    payload=raw if exit_code==0 else failed_terminal(raw)
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:calls.append(kw) or (exit_code,json.dumps(payload)))
    result=profiles.service_profiles_apply_once(SimpleNamespace(cluster_id=None),component='docker',dry_run=dry_run)
    assert result['status']==outer and result['apply']==raw
    assert calls[0]['args']==fetched['external_handler']['dry_run_command' if dry_run else 'command']['argv'][6:]


def test_docker_missing_descriptor_does_not_run_retained_reference_writer(monkeypatch):
    monkeypatch.setattr(profiles,'fetch_service_profile',lambda *a,**kw:{'status':'ok','component':'docker','profile':{}})
    assert not hasattr(profiles,'apply_docker_profile')
    with pytest.raises(ValueError,match='external_handler_missing'):
        profiles.service_profiles_apply_once(SimpleNamespace(cluster_id=None),component='docker')
