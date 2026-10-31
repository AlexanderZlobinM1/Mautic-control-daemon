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


def test_existing_post_advertises_actual_capabilities_without_candidate_opt_in(monkeypatch):
    cfg=SimpleNamespace(mcc_url='https://controller.invalid',mcc_token='test')
    monkeypatch.setattr(profiles,'resolve_agent_identity',lambda cfg:{'effective_hostname':'disposable'})
    calls=[]
    monkeypatch.setattr(profiles,'_post_json',lambda url,payload,*a,**kw:calls.append((url,payload)) or {'status':'ok'})
    profiles.fetch_service_profile(cfg,'php_fpm')
    url,payload=calls[0]
    assert url.endswith('/api/v1/agent/service-profile/php-fpm')
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
