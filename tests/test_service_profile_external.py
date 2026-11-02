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


@pytest.mark.parametrize('component',['php_fpm','mysql','docker','wazuh','apt'])
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


def test_offline_service_profile_reads_only_explicit_portal_cache_descriptor(tmp_path):
    profile = {"repo": "mariadb_11_4", "refresh_cache": True}
    raw = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    cache = tmp_path / "custom"
    cache.mkdir()
    (cache / "profiles").mkdir()
    (cache / "profiles" / "apt.json").write_bytes(raw)
    manifest = {
        "manifest_version": 1,
        "items": [],
        "operation_bindings": [{
            "component": "apt", "operation": "apt_apply",
            "profile_source": {
                "type": "cached-json", "file": "profiles/apt.json",
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
        }],
    }
    (cache / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    cfg = SimpleNamespace(mcc_url=None, custom_cache_dir=str(cache))

    result = profiles.fetch_service_profile(
        cfg, "apt", include_runtime_input=True, cache_operation="apt_apply",
    )

    assert result["status"] == "ok"
    assert result["profile"] == profile
    assert result["profile_source"]["sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("mutation,reason", [
    ("digest", "custom_operation_profile_checksum_mismatch"),
    ("path", "custom_operation_profile_descriptor_invalid"),
    ("intermediate_symlink", "custom_operation_profile_descriptor_invalid"),
    ("duplicate", "custom_operation_profile_descriptor_ambiguous"),
    ("manifest", "custom_operation_profile_manifest_invalid"),
    ("manifest_symlink", "custom_operation_profile_manifest_invalid"),
])
def test_offline_profile_cache_is_fail_closed(tmp_path, mutation, reason):
    raw = b'{"repo":"mariadb_11_4"}'
    cache = tmp_path / "custom"
    cache.mkdir()
    (cache / "profiles.json").write_bytes(raw)
    source = {"type": "cached-json", "file": "profiles.json", "sha256": hashlib.sha256(raw).hexdigest()}
    bindings = [{"component": "apt", "operation": "apt_apply", "profile_source": source}]
    if mutation == "digest":
        source["sha256"] = "0" * 64
    elif mutation == "path":
        source["file"] = "../profiles.json"
    elif mutation == "intermediate_symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "profile.json").write_bytes(raw)
        (cache / "link").symlink_to(outside, target_is_directory=True)
        source["file"] = "link/profile.json"
    elif mutation == "duplicate":
        bindings.append(dict(bindings[0]))
    else:
        bindings = "broken"
    if mutation == "manifest_symlink":
        (cache / "manifest.json").symlink_to(cache / "missing-manifest.json")
    else:
        (cache / "manifest.json").write_text(json.dumps({
            "operation_bindings": bindings,
        }), encoding="utf-8")
    cfg = SimpleNamespace(mcc_url=None, custom_cache_dir=str(cache))

    result = profiles.fetch_service_profile(
        cfg, "apt", include_runtime_input=True, cache_operation="apt_apply",
    )

    assert result == {"status": "error", "reason": reason}


def test_offline_profile_cache_is_not_used_when_mcc_is_configured(monkeypatch, tmp_path):
    cache = tmp_path / "custom"
    cache.mkdir()
    (cache / "manifest.json").write_text(json.dumps({"operation_bindings": []}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(profiles, "_post_json", lambda *a, **kw: calls.append(a) or {"status": "ok"})
    monkeypatch.setattr(profiles, "resolve_agent_identity", lambda _cfg: {
        "effective_hostname": "host", "effective_mcc_host_name": "", "configured_host_name": None,
        "local_hostname": "host",
    })
    cfg = SimpleNamespace(mcc_url="https://portal.invalid", mcc_token="unused", custom_cache_dir=str(cache))

    result = profiles.fetch_service_profile(cfg, "apt", cache_operation="apt_apply")

    assert result["status"] == "ok"
    assert len(calls) == 1


def test_apt_owner_binding_runs_as_one_external_operation_without_builtin_composition(fetched, monkeypatch):
    fetched.update(component='apt', resolved_plan_key='apt:profile')
    monkeypatch.setattr(profiles, 'fetch_service_profile', lambda *a, **kw: fetched)
    monkeypatch.setattr(profiles.os, 'geteuid', lambda: 0)
    calls = []
    monkeypatch.setattr(
        profiles, '_configured_apt_apply',
        lambda *args, **kwargs: calls.append((args, kwargs)) or {'status': 'applied', 'actions': ['owner-composition']},
    )
    monkeypatch.setattr(profiles, 'apply_apt_profile', lambda *a, **kw: pytest.fail('builtin must not compose selected owner operation'))
    monkeypatch.setattr(profiles, '_configured_zabbix_bootstrap', lambda *a, **kw: pytest.fail('owner composition already owns monitor stage'))
    result = profiles.service_profiles_apply_once(
        SimpleNamespace(cluster_id=None), component='apt', dry_run=False,
    )

    assert result['status'] == 'ok'
    assert len(calls) == 1
    assert calls[0][1]['dry_run'] is False
    assert result['apply'] == {'status': 'applied', 'actions': ['owner-composition']}


def test_apt_external_failure_does_not_fallback_to_builtin(fetched, monkeypatch):
    fetched.update(component='apt', resolved_plan_key='apt:profile')
    monkeypatch.setattr(profiles, 'fetch_service_profile', lambda *a, **kw: fetched)
    monkeypatch.setattr(profiles.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(
        profiles, '_configured_apt_apply',
        lambda *args, **kwargs: {'status': 'error', 'reason': 'owner_composition_failed'},
    )
    monkeypatch.setattr(profiles, 'apply_apt_profile', lambda *a, **kw: pytest.fail('must not fall back'))

    result = profiles.service_profiles_apply_once(
        SimpleNamespace(cluster_id=None), component='apt', dry_run=True,
    )

    assert result['status'] == 'error'
    assert result['apply']['reason'] == 'owner_composition_failed'


def test_configured_operation_preserves_typed_result_from_failed_terminal(fetched, monkeypatch):
    terminal = failed_terminal({
        'status': 'error',
        'reason': 'apt_profile_repo_failed',
        'errors': ['repo profile failed'],
        'state': {'marker_retained': True},
    })
    monkeypatch.setattr(
        profiles,
        'run_configured_external_operation',
        lambda *args, **kwargs: (1, json.dumps(terminal)),
    )

    result = profiles._configured_apt_operation(
        fetched['profile'],
        cfg=SimpleNamespace(),
        operation='apt_apply',
        operation_input={'dry_run': False, 'force_repo_rescan': False},
    )

    assert result == terminal['result']
    assert result['reason'] == 'apt_profile_repo_failed'

    terminal['result']['status'] = 'applied'
    monkeypatch.setattr(
        profiles,
        'run_configured_external_operation',
        lambda *args, **kwargs: (1, json.dumps(terminal)),
    )
    result = profiles._configured_apt_operation(
        fetched['profile'],
        cfg=SimpleNamespace(),
        operation='apt_apply',
        operation_input={'dry_run': False, 'force_repo_rescan': False},
    )
    assert result['status'] == 'error'
    assert result['reason'] == 'apt_profile_repo_failed'


def test_configured_apt_binding_preserves_exact_private_input_contract(tmp_path, monkeypatch):
    from mcd_agent import custom_scripts as custom

    root = tmp_path / 'custom'
    root.mkdir()
    digest = 'b' * 64
    binding = {
        'component': 'apt',
        'operation': 'apt_apply',
        'artifact': {'key': 'apt-full-composition-v1.0.0', 'sha256': digest, 'entrypoint': 'block.py'},
        'args': ['--runtime-input-stdin'],
        'capture_result': True,
        'detach': False,
        'live_output': False,
        'offline_only': True,
        'runtime_input_schema': {
            'type': 'string-map',
            'fields': ['operator_profile_json', 'cfg_facts_json', 'operation_input_json'],
        },
        'runtime_input_mapping': {
            'operator_profile_json': {'encoding': 'json', 'source': 'existing_private_operator_profile', 'envelope_key': 'profile'},
            'cfg_facts_json': {'encoding': 'json', 'source': 'effective_caller_cfg', 'fields': [
                'state_db_path', 'state_mysql_host', 'state_mysql_port', 'state_mysql_unix_socket',
                'state_mysql_user', 'state_mysql_password',
            ]},
            'operation_input_json': {'encoding': 'json', 'source': 'caller_operation_input', 'fields': ['dry_run', 'force_repo_rescan']},
        },
    }
    (root / 'manifest.json').write_text(json.dumps({'operation_bindings': [binding]}))
    cfg = SimpleNamespace(
        custom_cache_dir=str(root), state_db_path='/var/lib/mcd/state.db',
        state_mysql_host='127.0.0.1', state_mysql_port=3306,
        state_mysql_unix_socket='', state_mysql_user='mcd', state_mysql_password='secret',
    )
    calls = []
    monkeypatch.setattr(
        custom, 'run_custom_script_by_key',
        lambda config, **kwargs: calls.append(kwargs) or (0, '{"status":"planned"}'),
    )

    code, output = custom.run_configured_external_operation(
        cfg,
        component='apt',
        operation='apt_apply',
        profile={'x': 1},
        operation_input={'dry_run': False, 'force_repo_rescan': True},
    )

    assert code == 0 and json.loads(output) == {'status': 'planned'}
    assert len(calls) == 1
    assert json.loads(calls[0]['runtime_input']['operator_profile_json']) == {'profile': {'x': 1}}
    assert json.loads(calls[0]['runtime_input']['cfg_facts_json']) == {
        'state_db_path': '/var/lib/mcd/state.db', 'state_mysql_host': '127.0.0.1',
        'state_mysql_port': 3306, 'state_mysql_unix_socket': '',
        'state_mysql_user': 'mcd', 'state_mysql_password': 'secret',
    }
    assert json.loads(calls[0]['runtime_input']['operation_input_json']) == {
        'dry_run': False, 'force_repo_rescan': True,
    }


def test_unselected_apt_handler_keeps_explicit_legacy_compatibility_path(fetched, monkeypatch):
    fetched.update(component='apt', resolved_plan_key='apt:legacy')
    fetched['external_handler'].update(component='apt', resolved_plan_key='apt:legacy', state='candidate', active=False)
    monkeypatch.setattr(profiles, 'fetch_service_profile', lambda *a, **kw: fetched)
    monkeypatch.setattr(profiles.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(profiles, 'apply_apt_profile', lambda *a, **kw: {'status': 'planned', 'actions': []})
    monkeypatch.setattr(profiles, 'apply_external_service_profile', lambda *a, **kw: pytest.fail('candidate apt handler must not execute'))
    result = profiles.service_profiles_apply_once(SimpleNamespace(cluster_id=None), component='apt', dry_run=True)
    assert result['status'] == 'ok'
    assert result['apply'] == {'status': 'planned', 'actions': []}


def test_apt_monitor_does_not_bypass_early_apply_gate(fetched, monkeypatch):
    fetched.update(component='apt', resolved_plan_key='apt:zabbix-monitor-bootstrap')
    fetched['external_handler'].update(component='apt', resolved_plan_key='apt:zabbix-monitor-bootstrap')
    monkeypatch.setattr(profiles, 'fetch_service_profile', lambda *a, **kw: fetched)
    monkeypatch.setattr(profiles.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(
        profiles,
        'apply_apt_profile',
        lambda *a, **kw: {
            'status': 'error',
            'reason': 'apt_profile_repo_failed',
            'actions': [],
            'changed_files': [],
        },
    )
    calls = []
    monkeypatch.setattr(
        profiles,
        '_configured_zabbix_bootstrap',
        lambda *a, **kw: calls.append((a, kw)) or {'status': 'already_present'},
    )

    result = profiles.service_profiles_apply_once(SimpleNamespace(cluster_id=None), component='apt')

    assert result['status'] == 'error'
    assert calls == []
    assert result['apply']['zabbix_mysql_monitor'] == {
        'status': 'skipped',
        'reason': 'apt_profile_gate_failed',
        'apt_profile_status': 'error',
    }


def test_apt_monitor_runs_at_the_former_reached_operation_step(fetched, monkeypatch):
    fetched.update(component='apt', resolved_plan_key='apt:zabbix-monitor-bootstrap')
    fetched['external_handler'].update(component='apt', resolved_plan_key='apt:zabbix-monitor-bootstrap')
    monkeypatch.setattr(profiles, 'fetch_service_profile', lambda *a, **kw: fetched)
    monkeypatch.setattr(profiles.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(
        profiles,
        'apply_apt_profile',
        lambda *a, **kw: {'status': 'applied', 'actions': [], 'state': {}},
    )
    calls = []
    monkeypatch.setattr(
        profiles,
        '_configured_zabbix_bootstrap',
        lambda *a, **kw: calls.append((a, kw)) or {'status': 'already_present'},
    )

    result = profiles.service_profiles_apply_once(SimpleNamespace(cluster_id=None), component='apt')

    assert result['status'] == 'ok'
    assert len(calls) == 1
    assert result['apply']['zabbix_mysql_monitor'] == {'status': 'already_present'}


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


def test_descriptor_declared_private_string_map_uses_generic_stdin_seam(fetched, monkeypatch):
    fetched.update(component='apt', resolved_plan_key='apt:zabbix-monitor-bootstrap')
    fetched['external_handler'].update(
        component='apt', resolved_plan_key='apt:zabbix-monitor-bootstrap',
        runtime_input_schema={
            'type': 'string-map',
            'fields': ['state_dir', 'mysql_password'],
        },
    )
    private = {'state_dir': '/opt/mcd/var', 'mysql_password': 'SYNTHETIC-PRIVATE'}
    fetched['runtime_input'] = private
    for field in ('command', 'dry_run_command'):
        fetched['external_handler'][field]['argv'].append('--runtime-input-stdin')
    calls = []
    monkeypatch.setattr(ext, 'run_custom_script_by_key', lambda cfg, **kw: calls.append(kw) or (0, '{"status":"applied"}'))
    result = ext.apply_external_service_profile(SimpleNamespace(), fetched, component='apt')
    assert result == {'status': 'applied'}
    assert calls[0]['runtime_input'] == private
    assert private['mysql_password'] not in str(calls[0]['args'])
    assert 'runtime_input' not in profiles.public_service_profile_response(fetched)


@pytest.mark.parametrize('schema', [
    {'type': 'string-map', 'fields': []},
    {'type': 'string-map', 'fields': ['x', 'x']},
    {'type': 'object', 'fields': ['x']},
])
def test_descriptor_declared_private_string_map_rejects_invalid_schema(fetched, schema):
    fetched.update(component='apt', resolved_plan_key='apt:zabbix-monitor-bootstrap')
    fetched['external_handler'].update(
        component='apt', resolved_plan_key='apt:zabbix-monitor-bootstrap',
        runtime_input_schema=schema,
    )
    fetched['runtime_input'] = {'x': 'SYNTHETIC-PRIVATE'}
    fetched['external_handler']['command']['argv'].append('--runtime-input-stdin')
    with pytest.raises(ValueError, match='external_handler_runtime_input_schema_invalid'):
        ext.apply_external_service_profile(SimpleNamespace(), fetched, component='apt')


def test_descriptor_declared_private_string_map_requires_exact_fields(fetched):
    fetched.update(component='apt', resolved_plan_key='apt:zabbix-monitor-bootstrap')
    fetched['external_handler'].update(
        component='apt', resolved_plan_key='apt:zabbix-monitor-bootstrap',
        runtime_input_schema={'type': 'string-map', 'fields': ['x']},
    )
    fetched['runtime_input'] = {'x': 'SYNTHETIC-PRIVATE', 'extra': 'SYNTHETIC-PRIVATE'}
    fetched['external_handler']['command']['argv'].append('--runtime-input-stdin')
    with pytest.raises(ValueError, match='external_handler_runtime_input_invalid'):
        ext.apply_external_service_profile(SimpleNamespace(), fetched, component='apt')


@pytest.mark.parametrize('problem',['capability','flag','null','wrongtype','extra','profile','invalidutf8'])
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
    elif problem=='invalidutf8':wazuh_fetched['runtime_input']['registration_password']='\ud800'
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


@pytest.fixture
def nginx_fetched(fetched,monkeypatch):
    from mcd_agent import nginx_baseline
    monkeypatch.setattr(nginx_baseline,'_ipv6_listen_forbidden',lambda:False)
    fetched.update(component='nginx',operation='default_stub_operation',operation_input={'apply':False},resolved_plan_key='nginx:test',profile={},runtime_context={'ipv6_listen_forbidden':False})
    h=fetched['external_handler'];h.update(component='nginx',operation=fetched['operation'],operation_input=copy.deepcopy(fetched['operation_input']),resolved_plan_key='nginx:test',runtime_context=copy.deepcopy(fetched['runtime_context']))
    h['minimum_runtime']['required_capabilities']=sorted(ext.BASE_RUNTIME_CAPABILITIES)
    h['runtime_context_input']={'sha256':hashlib.sha256(canonical_json(h['runtime_context'])).hexdigest()}
    h['operation_input_input']={'sha256':hashlib.sha256(canonical_json(h['operation_input'])).hexdigest()}
    h['profile_input']={'source':'response.profile','sha256':hashlib.sha256(b'{}').hexdigest(),'fields':[]}
    del h['dry_run_command']
    return fetched


@pytest.mark.parametrize('operation,flags',[
    ('default_stub_operation',{'apply':False}),('default_stub_operation',{'apply':True}),
    ('ensure_nginx_baseline',{'reload_service':False}),('ensure_cloudflare_real_ip',{'reload_service':True}),
    ('nginx_baseline_satisfied',{}),('cloudflare_real_ip_state',{}),
    ('write_nginx_vhost',{'root':'/explicit-qa-root','domains':['qa.example.invalid'],'php_version':'8.5'}),
    ('write_switched_vhost',{'source_root':'/explicit-old','nginx_root':'/explicit-new/docroot','php_version':'8.5','site_available':'/explicit-site.conf','site_enabled':None})])
def test_nginx_operation_and_input_are_bound_before_same_offline_runner(nginx_fetched,monkeypatch,operation,flags):
    nginx_fetched.update(operation=operation,operation_input=flags)
    if operation in {'write_nginx_vhost','write_switched_vhost'}:
        nginx_fetched['profile']=None
        nginx_fetched['external_handler']['profile_input']['sha256']=hashlib.sha256(b'null').hexdigest()
    nginx_fetched['external_handler'].update(operation=operation,operation_input=copy.deepcopy(flags))
    nginx_fetched['external_handler']['operation_input_input']['sha256']=hashlib.sha256(canonical_json(flags)).hexdigest()
    calls=[]
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:calls.append(kw) or (0,'{"status":"ok"}'))
    assert ext.apply_external_service_profile(None,nginx_fetched,component='nginx',offline_only=True)=={'status':'ok'}
    assert calls[0]['offline_only'] is True and 'runtime_input' not in calls[0]


@pytest.mark.parametrize('problem',['operation','extra_flag','wrong_flag_type','handler_bool_coercion','fact','projection','profile_pin','private'])
def test_nginx_mismatch_rejected_before_artifact_or_any_process(nginx_fetched,monkeypatch,problem):
    if problem=='operation':nginx_fetched['operation']='unknown'
    elif problem=='extra_flag':nginx_fetched['operation_input']['other']=True
    elif problem=='wrong_flag_type':nginx_fetched['operation_input']['apply']=0
    elif problem=='handler_bool_coercion':nginx_fetched['external_handler']['operation_input']['apply']=0
    elif problem=='fact':nginx_fetched['external_handler']['runtime_context']['ipv6_listen_forbidden']=True
    elif problem=='projection':nginx_fetched['external_handler']['profile_input']['fields']=None
    elif problem=='profile_pin':nginx_fetched['profile']['unexpected']=1
    elif problem=='private':nginx_fetched['runtime_input']={'registration_password':'synthetic-only'}
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:pytest.fail('must reject before process/cache/download'))
    with pytest.raises(ValueError):ext.apply_external_service_profile(None,nginx_fetched,component='nginx')


def test_nginx_none_observer_profile_is_distinct_from_empty_mapping(nginx_fetched,monkeypatch):
    nginx_fetched.update(operation='cloudflare_real_ip_state',operation_input={},profile=None)
    h=nginx_fetched['external_handler'];h.update(operation='cloudflare_real_ip_state',operation_input={})
    h['operation_input_input']['sha256']=hashlib.sha256(b'{}').hexdigest()
    h['profile_input']['sha256']=hashlib.sha256(b'null').hexdigest()
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:(0,'{"status":"absent"}'))
    assert ext.apply_external_service_profile(None,nginx_fetched,component='nginx')=={'status':'absent'}
    nginx_fetched['profile']={}
    with pytest.raises(ValueError,match='profile_pin_mismatch'):ext.apply_external_service_profile(None,nginx_fetched,component='nginx')


def test_nginx_validation_only_and_unsupported_dry_run_never_execute(nginx_fetched,monkeypatch):
    monkeypatch.setattr(ext,'run_custom_script_by_key',lambda *a,**kw:pytest.fail('admission has no recipe action'))
    ext.validate_external_service_profile(None,nginx_fetched,component='nginx')
    with pytest.raises(ValueError,match='dry_run_unavailable'):ext.apply_external_service_profile(None,nginx_fetched,component='nginx',dry_run=True)


@pytest.mark.parametrize('present',[False,True])
def test_nginx_online_first_sync_preserves_actual_null_mapping_contract_and_prefetches_before_selection(tmp_path,monkeypatch,present):
    from pathlib import Path
    from mcd_agent import custom_scripts as custom,nginx_baseline
    fixture=json.loads((Path(__file__).parent/'fixtures/nginx-inactive-observer-responses-v1.json').read_text())
    responses=fixture['responses'];response=copy.deepcopy(responses['profile_present_true' if present else 'profile_present_false'])
    response['external_handler'].update(active=True,state='active',activation_required=False)  # unit-only operator fixture, not live activation
    root=tmp_path/'cache';root.mkdir()
    rows=[{**r['external_handler']['artifact'],'interactive':False,'run_mode':'direct'} for r in responses.values()]
    (root/'manifest.json').write_text(json.dumps({'items':rows}))
    cfg=SimpleNamespace(mcc_url='https://synthetic.invalid',mcc_token='synthetic-test-token',custom_cache_dir=str(root))
    monkeypatch.setattr(profiles,'resolve_agent_identity',lambda cfg:{'local_hostname':'synthetic-local'})
    monkeypatch.setattr(nginx_baseline,'_ipv6_listen_forbidden',lambda:False)
    events=[]
    def api(url,payload,token,**kwargs):
        assert payload['profile_present'] is present and payload['operation_input']=={}
        assert payload['ipv6_listen_forbidden'] is False and token==cfg.mcc_token
        events.append('authenticated-response-fixture')
        return copy.deepcopy(response)
    def prefetch(*a,**kwargs):
        assert kwargs['fetch_only'] is True
        assert 'selected_tasks' not in json.loads((root/'manifest.json').read_text())
        events.append('verified-artifact-prefetch')
        return 0,'{"status":"cached"}'
    monkeypatch.setattr(profiles,'_post_json',api);monkeypatch.setattr(custom,'run_custom_script_by_key',prefetch)
    fetched=profiles.fetch_service_profile(cfg,'nginx',operation='cloudflare_real_ip_state',operation_input={},profile_present=present)
    assert fetched['status']=='ok' and (fetched['profile'] is not None) is present
    selected=custom.cached_selected_task(cfg,component='nginx',operation='cloudflare_real_ip_state',operation_input={},profile={} if present else None,runtime_context={'ipv6_listen_forbidden':False})
    assert selected==fetched and events==['authenticated-response-fixture','verified-artifact-prefetch']


def test_nginx_inactive_api_response_does_not_prefetch_or_create_selection_cache(tmp_path,monkeypatch):
    from pathlib import Path
    from mcd_agent import custom_scripts as custom,nginx_baseline
    fixture=json.loads((Path(__file__).parent/'fixtures/nginx-inactive-observer-responses-v1.json').read_text())
    response=fixture['responses']['profile_present_false']
    cfg=SimpleNamespace(mcc_url='https://synthetic.invalid',mcc_token='synthetic',custom_cache_dir=str(tmp_path/'absent'))
    monkeypatch.setattr(profiles,'resolve_agent_identity',lambda cfg:{'local_hostname':'synthetic-local'})
    monkeypatch.setattr(nginx_baseline,'_ipv6_listen_forbidden',lambda:False)
    monkeypatch.setattr(profiles,'_post_json',lambda *a,**kw:response)
    monkeypatch.setattr(custom,'run_custom_script_by_key',lambda *a,**kw:pytest.fail('inactive must not prefetch/execute'))
    assert profiles.fetch_service_profile(cfg,'nginx',operation='cloudflare_real_ip_state',operation_input={},profile_present=False)['external_handler']['active'] is False
    assert not (tmp_path/'absent').exists()
