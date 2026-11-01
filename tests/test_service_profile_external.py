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


@pytest.mark.parametrize('component',['php_fpm','mysql','docker','wazuh'])
def test_existing_post_advertises_actual_capabilities_without_candidate_opt_in(monkeypatch,component):
    cfg=SimpleNamespace(mcc_url='https://controller.invalid',mcc_token='test')
    monkeypatch.setattr(profiles,'resolve_agent_identity',lambda cfg:{'effective_hostname':'disposable','effective_mcc_host_name':'','configured_host_name':None,'local_hostname':'disposable'})
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


@pytest.fixture
def wazuh_fetched(fetched):
    context={'effective_mcc_host_name':None,'configured_host_name':None,'local_hostname':'observed-local'}
    profile={'enabled':True,'agent_name_source':'mcc_host_name'}
    fetched.update(component='wazuh',resolved_plan_key='wazuh:default',profile=profile,runtime_context=copy.deepcopy(context))
    fetched['external_handler'].update(component='wazuh',resolved_plan_key='wazuh:default',runtime_context=context,
        profile_input={'source':'response.profile','sha256':hashlib.sha256(canonical_json(profile)).hexdigest()})
    return fetched


@pytest.mark.parametrize('source',['mcc_host_name','configured_host_name','local_hostname','legacy-unknown-source'])
def test_wazuh_identity_transports_exact_nullable_facts_without_changing_old_selector_policy(wazuh_fetched,monkeypatch,source):
    profile=wazuh_fetched['profile'];profile['agent_name_source']=source
    wazuh_fetched['external_handler']['profile_input']['sha256']=hashlib.sha256(canonical_json(profile)).hexdigest()
    context=copy.deepcopy(wazuh_fetched['external_handler']['runtime_context'])
    monkeypatch.setattr(ext,'resolve_agent_identity',lambda cfg:{**context,'effective_hostname':'must-not-replace-local'})
    calls=[]
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:calls.append(kw) or (0,'{"status":"planned"}'))
    assert ext.apply_external_service_profile(object(),wazuh_fetched,component='wazuh',dry_run=True)=={'status':'planned'}
    assert len(calls)==1 and wazuh_fetched['external_handler']['runtime_context']==context
    assert context['effective_mcc_host_name'] is None and context['configured_host_name'] is None


@pytest.mark.parametrize('key',['effective_mcc_host_name','configured_host_name','local_hostname'])
def test_wazuh_missing_identity_key_cannot_be_filled_from_another_name(wazuh_fetched,monkeypatch,key):
    del wazuh_fetched['external_handler']['runtime_context'][key]
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:pytest.fail('missing fact before download/execution'))
    with pytest.raises(ValueError,match='runtime_context_missing'):
        ext.apply_external_service_profile(None,wazuh_fetched,component='wazuh')


@pytest.mark.parametrize('key,value',[
    ('effective_mcc_host_name',False),('effective_mcc_host_name',1),
    ('configured_host_name',[]),('configured_host_name',{}),
    ('local_hostname',None),('local_hostname',''),('local_hostname','  '),('local_hostname',True),
])
def test_wazuh_invalid_identity_types_never_execute(wazuh_fetched,monkeypatch,key,value):
    wazuh_fetched['external_handler']['runtime_context'][key]=value
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:pytest.fail('invalid context before execution'))
    with pytest.raises(ValueError,match='runtime_context_invalid'):
        ext.apply_external_service_profile(None,wazuh_fetched,component='wazuh')


@pytest.mark.parametrize('key',['effective_mcc_host_name','configured_host_name','local_hostname'])
def test_wazuh_current_identity_drift_blocks_execution(wazuh_fetched,monkeypatch,key):
    actual={**wazuh_fetched['runtime_context'],key:'changed-current-fact'}
    monkeypatch.setattr(ext,'resolve_agent_identity',lambda cfg:actual)
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:pytest.fail('stale fact before download/execution'))
    with pytest.raises(ValueError,match='runtime_context_mismatch'):
        ext.apply_external_service_profile(None,wazuh_fetched,component='wazuh')


def test_wazuh_response_and_descriptor_context_must_agree(wazuh_fetched,monkeypatch):
    wazuh_fetched['runtime_context']['local_hostname']='different-response'
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:pytest.fail('mixed descriptor response'))
    with pytest.raises(ValueError,match='runtime_context_response_mismatch'):
        ext.apply_external_service_profile(None,wazuh_fetched,component='wazuh')


def test_wazuh_unavailable_current_fact_is_not_inferred_as_null(wazuh_fetched,monkeypatch):
    actual=dict(wazuh_fetched['runtime_context']);del actual['configured_host_name']
    monkeypatch.setattr(ext,'resolve_agent_identity',lambda cfg:actual)
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:pytest.fail('core fact unavailable'))
    with pytest.raises(ValueError,match='runtime_context_unavailable'):
        ext.apply_external_service_profile(None,wazuh_fetched,component='wazuh')


def test_wazuh_external_dispatch_preserves_failed_public_dictionary_without_builtin_fallback(wazuh_fetched,monkeypatch):
    raw={'status':'error','reason':'manager_unreachable','actions':['wazuh_repo:ok']}
    monkeypatch.setattr(ext,'resolve_agent_identity',lambda cfg:wazuh_fetched['runtime_context'])
    monkeypatch.setattr(profiles,'fetch_service_profile',lambda *a,**kw:wazuh_fetched)
    assert not hasattr(profiles,'apply_wazuh_profile')
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:(1,json.dumps(failed_terminal(raw))))
    result=profiles.service_profiles_apply_once(SimpleNamespace(cluster_id=None),component='wazuh')
    assert result['status']=='error' and result['reason']=='manager_unreachable' and result['apply']==raw


@pytest.mark.parametrize('component,expected',[('php_fpm',''),('wazuh',None)])
def test_wazuh_fetch_preserves_null_identity_without_changing_legacy_envelopes(monkeypatch,component,expected):
    cfg=SimpleNamespace(mcc_url='https://controller.invalid',mcc_token='test')
    identity={'effective_hostname':'observed-local','effective_mcc_host_name':'','configured_host_name':None,'local_hostname':'observed-local'}
    monkeypatch.setattr(profiles,'resolve_agent_identity',lambda cfg:identity)
    calls=[]
    monkeypatch.setattr(profiles,'_post_json',lambda url,payload,*a,**kw:calls.append(payload) or {'status':'ok'})
    assert profiles.fetch_service_profile(cfg,component)['status']=='ok'
    assert calls[0]['configured_host_name']==expected
    assert calls[0]['agent_hostname']=='observed-local' and calls[0]['mcc_host_name']==''


@pytest.mark.parametrize('missing',['effective_mcc_host_name','configured_host_name','local_hostname'])
def test_wazuh_fetch_does_not_guess_missing_core_facts(monkeypatch,missing):
    cfg=SimpleNamespace(mcc_url='https://controller.invalid',mcc_token='test')
    identity={'effective_hostname':'effective-is-not-local','effective_mcc_host_name':'','configured_host_name':None,'local_hostname':'observed-local'}
    del identity[missing]
    monkeypatch.setattr(profiles,'resolve_agent_identity',lambda cfg:identity)
    monkeypatch.setattr(profiles,'_post_json',lambda *a,**kw:pytest.fail('missing fact must not be invented for request'))
    assert profiles.fetch_service_profile(cfg,'wazuh')=={'status':'error','reason':'wazuh_identity_context_unavailable'}


def test_private_runtime_input_is_memory_only_and_public_fetch_omits_it(wazuh_fetched, monkeypatch):
    private = {'registration_password':'  SYNTHETIC:é"\\\t\n\n'}
    wazuh_fetched['runtime_input'] = private
    for field in ('command','dry_run_command'):
        wazuh_fetched['external_handler'][field]['argv'].append('--runtime-input-stdin')
    monkeypatch.setattr(ext,'resolve_agent_identity',lambda cfg:wazuh_fetched['external_handler']['runtime_context'])
    calls=[]
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:calls.append(kw) or (0,'{"status":"applied"}'))
    monkeypatch.setattr(profiles,'fetch_service_profile',lambda *a,**kw:wazuh_fetched)
    result=profiles.service_profiles_apply_once(SimpleNamespace(cluster_id=None),component='wazuh')
    assert result['status']=='ok'
    assert 'runtime_input' not in result['fetch']
    assert private['registration_password'] not in json.dumps(result)
    assert calls[0]['runtime_input']==private
    assert private['registration_password'] not in str(calls[0]['args'])
    assert wazuh_fetched['runtime_input']==private


@pytest.mark.parametrize('problem',['capability','flag','null','wrongtype','extra','profile'])
def test_private_runtime_input_rejected_before_artifact_download(wazuh_fetched,monkeypatch,problem):
    wazuh_fetched['runtime_input']={'registration_password':'SYNTHETIC-PRIVATE'}
    h=wazuh_fetched['external_handler']
    for field in ('command','dry_run_command'):h[field]['argv'].append('--runtime-input-stdin')
    if problem=='capability':h['minimum_runtime']['required_capabilities']=sorted(ext.BASE_RUNTIME_CAPABILITIES)
    elif problem=='flag':h['command']['argv'].remove('--runtime-input-stdin')
    elif problem=='null':wazuh_fetched['runtime_input']=None
    elif problem=='wrongtype':wazuh_fetched['runtime_input']['registration_password']=42
    elif problem=='extra':wazuh_fetched['runtime_input']['other']='bad'
    elif problem=='profile':wazuh_fetched['profile']['registration_password']='SYNTHETIC-PRIVATE'
    monkeypatch.setattr(ext,'resolve_agent_identity',lambda cfg:h['runtime_context'])
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:pytest.fail('reject before cache/download'))
    with pytest.raises(ValueError):ext.apply_external_service_profile(None,wazuh_fetched,component='wazuh')


def test_public_fetch_never_returns_runtime_input(monkeypatch):
    raw={'status':'ok','profile':{},'runtime_input':{'registration_password':'SYNTHETIC-PRIVATE'}}
    monkeypatch.setattr(profiles,'resolve_agent_identity',lambda cfg:{'effective_mcc_host_name':None,'configured_host_name':None,'local_hostname':'local'})
    monkeypatch.setattr(profiles,'_post_json',lambda *a,**kw:raw)
    cfg=SimpleNamespace(mcc_url='https://synthetic.example.invalid',mcc_token='synthetic')
    assert 'runtime_input' not in profiles.fetch_service_profile(cfg,'wazuh')
    assert profiles.fetch_service_profile(cfg,'wazuh',include_runtime_input=True)['runtime_input']==raw['runtime_input']


def test_no_secret_descriptor_keeps_original_two_capabilities(wazuh_fetched,monkeypatch):
    wazuh_fetched['external_handler']['minimum_runtime']['required_capabilities']=sorted(ext.BASE_RUNTIME_CAPABILITIES)
    monkeypatch.setattr(ext,'resolve_agent_identity',lambda cfg:wazuh_fetched['external_handler']['runtime_context'])
    def run(*a,**kw):
        assert 'runtime_input' not in kw
        return 0,'{"status":"noop"}'
    monkeypatch.setattr(ext,'run_custom_script_by_key',run)
    assert ext.apply_external_service_profile(None,wazuh_fetched,component='wazuh')=={'status':'noop'}
