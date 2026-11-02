import copy,sqlite3
import pytest
from mcd_agent.commercial_portal import CommercialState,PortalClient,portal_state_identity
from mcd_agent.commercial_registration import import_activation_material,RegistrationMaterialError
from mcd_agent.commercial_self_hosted import SelfHostedPortalWorker,SelfHostedScenarioExecutor

DNS1="one.example.invalid"
DNS2="two.example.invalid"
HOST="routing.example.invalid"
def material():
 rows={uid:{"installation_id":"installation-"+str(i),"instance_uid":uid,"root":"/srv/"+str(i),"dns_name":uid,"mcc_host_name":HOST,"mcc_job_id":"job-"+str(i),"order_id":"order-"+str(i),"account_id":"tenant-"+str(i)} for i,uid in enumerate([DNS1,DNS2],1)}
 return {"schema":"mcd-portal-enrollment-material-v1","identity_kind":"dns","local_identity":HOST,"ownership":"sales_snap_managed","account_id":"account","agent_id":"agent","commercial_key_id":"key-id","portal_agent_credential":"unit-fixture-only","portal_api_base":"https://portal.example.invalid/api/v1/commercial","installation_ids":{uid:r["installation_id"] for uid,r in rows.items()},"managed_instances":rows}
def state(tmp_path):
 s=CommercialState(str(tmp_path/"state.db"))
 with sqlite3.connect(s.path) as db:
  db.execute("CREATE TABLE instances(instance_uid TEXT,root TEXT,primary_domain TEXT,domains_json TEXT)")
  db.executemany("INSERT INTO instances VALUES (?,?,?,?)",[(DNS1,"/srv/1",DNS1,"[]"),(DNS2,"/srv/2",DNS2,"[]")])
 return s

def test_managed_import_and_worker_keep_each_instance_coordinate(tmp_path):
 s=state(tmp_path);m=material();import_activation_material(s,m);assert s.portal_enrollment_enabled
 client=PortalClient(s)
 assert client._commercial_binding("installation-1")["local_identity"]==DNS1
 assert client._commercial_binding("installation-2")["local_identity"]==DNS2
 worker=SelfHostedPortalWorker(client,SelfHostedScenarioExecutor(s),installation_id=["installation-1","installation-2"],portal_keys={},publisher_keys={},context={"identity_kind":"dns","local_identity":HOST},commercial_key_id="key-id",agent_id="agent",host_fingerprint="")
 worker._active_installation_id="installation-1";assert worker.context["local_identity"]==DNS1
 assert worker._channel_binding("installation-2")["local_identity"]==DNS2
 worker._active_installation_id="installation-2";assert worker.identity_coordinate["local_identity"]==DNS2
 assert s.get("local_identity")==HOST
 with pytest.raises(ValueError):client._commercial_binding("unknown")

def test_removed_instance_does_not_disable_other_instance(tmp_path):
 s=state(tmp_path);m=material();import_activation_material(s,m)
 del m["installation_ids"][DNS1];del m["managed_instances"][DNS1]
 import_activation_material(s,m);assert s.portal_enrollment_enabled
 assert portal_state_identity(s,"installation-2")["local_identity"]==DNS2

@pytest.mark.parametrize("fault",["root","uid","key","duplicate"])
def test_invalid_managed_binding_does_not_overwrite_existing(tmp_path,fault):
 s=state(tmp_path);m=material();import_activation_material(s,m);bad=copy.deepcopy(m)
 if fault=="root":bad["managed_instances"][DNS1]["root"]="/srv/other"
 elif fault=="uid":bad["managed_instances"][DNS1]["instance_uid"]=DNS2
 elif fault=="key":bad["commercial_key_id"]="another-key"
 else:bad["installation_ids"][DNS2]="installation-1"
 with pytest.raises(RegistrationMaterialError):import_activation_material(s,bad)
 assert s.get("installation_ids")==m["installation_ids"] and s.get("commercial_key_id")=="key-id"


def test_unenrolled_fingerprint_retired_only_after_admission(tmp_path):
 s=state(tmp_path);s.put("host_fingerprint","legacy-unenrolled")
 bad=material();bad["managed_instances"][DNS1]["root"]="/wrong"
 with pytest.raises(RegistrationMaterialError):import_activation_material(s,bad)
 assert s.get("host_fingerprint")=="legacy-unenrolled" and not s.get("agent_id")
 import_activation_material(s,material())
 assert s.get("host_fingerprint") is None and s.portal_enrollment_enabled


def test_active_legacy_binding_preserved(tmp_path):
 s=state(tmp_path);s.put_many({"host_fingerprint":"legacy-active","agent_id":"old-agent","portal_agent_credential":"legacy-unit-fixture"})
 with pytest.raises(RegistrationMaterialError):import_activation_material(s,material())
 assert s.get("host_fingerprint")=="legacy-active" and s.get("agent_id")=="old-agent"


def test_only_explicit_managed_connect_reactivates_after_full_admission(tmp_path):
 s=state(tmp_path);s.put_many({"commercial_detached":True,"commercial_portal_connection_state":"detached"})
 m=material();import_activation_material(s,m)
 assert s.get("commercial_detached") is True
 bad=copy.deepcopy(m);bad["connection_state"]="active";bad["managed_instances"][DNS1]["root"]="/wrong"
 with pytest.raises(RegistrationMaterialError):import_activation_material(s,bad)
 assert s.get("commercial_detached") is True
 m["connection_state"]="active";import_activation_material(s,m)
 assert s.get("commercial_detached") is False and s.get("commercial_revoked") is False and s.get("commercial_portal_connection_state")=="active"


def test_native_uid_is_not_display_dns(tmp_path):
 from types import SimpleNamespace
 from mcd_agent.commercial_capabilities import resolve_portal_command_uid
 from mcd_agent.daemon import _commercial_registration_descriptor
 s=state(tmp_path);m=material();uid="opaque-native-instance-1"
 with sqlite3.connect(s.path) as db:db.execute("UPDATE instances SET instance_uid=? WHERE instance_uid=?",(uid,DNS1))
 row=m["managed_instances"].pop(DNS1);row["instance_uid"]=uid;m["managed_instances"][uid]=row
 m["installation_ids"][uid]=m["installation_ids"].pop(DNS1)
 import_activation_material(s,m)
 assert PortalClient(s)._commercial_binding("installation-1")["local_identity"]==DNS1
 assert _commercial_registration_descriptor(s,m["installation_ids"],"installation-1")["source_instance_id"]==uid
 job={"installation":{"installation_id":"installation-1","dns_name":DNS1,"account_id":"tenant-1"}}
 assert resolve_portal_command_uid(job,config=SimpleNamespace(state_db_path=s.path))==uid
 wrong={"installation":{"installation_id":"installation-1","dns_name":DNS2}}
 with pytest.raises(ValueError):resolve_portal_command_uid(wrong,config=SimpleNamespace(state_db_path=s.path))


def test_capability_cache_retains_both_installations_and_repairs_without_transport(tmp_path,monkeypatch):
 from mcd_agent.commercial_primitives import build_capability_inventory
 s=state(tmp_path);m=material();import_activation_material(s,m);client=PortalClient(s);sent=[]
 monkeypatch.setattr(client,"send_capability_inventory",lambda **kw:sent.append(kw) or {})
 provider=lambda ident:build_capability_inventory(installation_id=ident,dns_name=DNS1 if ident=="installation-1" else DNS2)
 worker=SelfHostedPortalWorker(client,SelfHostedScenarioExecutor(s),installation_id=["installation-1","installation-2"],portal_keys={},publisher_keys={},context={"identity_kind":"dns","local_identity":HOST},commercial_key_id="key-id",agent_id="agent",host_fingerprint="",capability_inventory_provider=provider)
 assert worker._publish_capabilities("installation-1") and worker._publish_capabilities("installation-2")
 cache=s.get("commercial_portal_generic_capabilities_v1");assert set(cache)=={"installation-1","installation-2"}
 del cache["installation-1"];s.put("commercial_portal_generic_capabilities_v1",cache)
 assert worker._publish_capabilities("installation-1") and len(sent)==2
 assert set(s.get("commercial_portal_generic_capabilities_v1"))=={"installation-1","installation-2"}


def test_managed_portal_cursors_are_scoped_to_installation(tmp_path):
 s=state(tmp_path);m=material();import_activation_material(s,m)
 s.put("commercial_portal_cursor", "11")
 worker=SelfHostedPortalWorker(PortalClient(s),SelfHostedScenarioExecutor(s),installation_id=["installation-1","installation-2"],portal_keys={},publisher_keys={},context={"identity_kind":"dns","local_identity":HOST},commercial_key_id="key-id",agent_id="agent",host_fingerprint="")
 assert worker._cursor_for("installation-1")==""
 worker._active_installation_id="installation-1";worker._finish_delivery(cursor_next="3")
 assert s.get("commercial_portal_cursors_v1")=={"installation-1":"3"}
 assert s.get("commercial_portal_cursor")=="11"
 assert worker._cursor_for("installation-1")=="3"


def test_managed_poll_request_state_is_scoped_to_installation(tmp_path):
 s=state(tmp_path);m=material();import_activation_material(s,m)
 s.put("commercial_portal_poll_requests_v1", {"installation-1": {"request_id":"old-a","idempotency_key":"old-a","cursor":"3"}})
 client=PortalClient(s)
 client._request=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stop-after-request-state"))
 with pytest.raises(RuntimeError, match="stop-after-request-state"):
  client.pull_job(installation_id="installation-2", cursor="", wait_seconds=0)
 requests=s.get("commercial_portal_poll_requests_v1")
 assert requests["installation-1"]["request_id"]=="old-a"
 assert requests["installation-2"]["cursor"]==""
 assert requests["installation-2"]["request_id"]!="old-a"


def test_capability_scope_wire_is_direct_for_portal_17(tmp_path, monkeypatch):
 from mcd_agent.commercial_primitives import build_capability_inventory
 s=state(tmp_path);m=material();import_activation_material(s,m);client=PortalClient(s);sent=[]
 monkeypatch.setattr(client,"send_capability_inventory",lambda **kw:sent.append(kw) or {"capability_features":["target_scope"]})
 provider=lambda ident:build_capability_inventory(installation_id=ident,dns_name=DNS1)
 worker=SelfHostedPortalWorker(client,SelfHostedScenarioExecutor(s),installation_id="installation-1",portal_keys={},publisher_keys={},context={"identity_kind":"dns","local_identity":HOST},commercial_key_id="key-id",agent_id="agent",host_fingerprint="",capability_inventory_provider=provider)
 assert worker._publish_capabilities("installation-1")
 assert any("target_scope" in command for capability in sent[-1]["inventory"]["capabilities"] for command in capability["commands"])
 assert worker._publish_capabilities("installation-1")
 assert len(sent)==1
