from types import SimpleNamespace
from mcd_agent.commercial_capabilities import execute_mautic_version,mautic_version_capability
from mcd_agent import cli
from mcd_agent import commercial_capabilities as core

def job():return {"input":{},"capability":{"capability_id":"mautic"},"command":{"command_id":"mautic_version"},"installation":{"dns_name":"one.example.invalid"}}

def test_version_reuses_registered_uid_and_existing_executor(monkeypatch):
 seen=[];cfg=SimpleNamespace(command_timeout_sec=123)
 monkeypatch.setattr(core,"resolve_portal_command_uid",lambda *a,**kw:"native-uid")
 def resolve(config,uid):assert config is cfg and uid=="native-uid";return "/registered/root","/observed/php","observed-user"
 monkeypatch.setattr(cli,"_resolve_registered_command_runtime",resolve)
 monkeypatch.setattr(cli,"_run_manual_command_with_scheduler",lambda **kw:seen.append(kw) or (0,"Mautic 7.1.3"))
 result=execute_mautic_version(job(),config=cfg)
 assert result["status"]=="succeeded" and result["output"]=="Mautic 7.1.3"
 assert seen[0]["command"]=="version" and seen[0]["root"]=="/registered/root" and seen[0]["timeout_sec"]==123

def test_version_rejects_caller_arguments_before_resolution(monkeypatch):
 monkeypatch.setattr(cli,"_resolve_registered_command_runtime",lambda *a:(_ for _ in ()).throw(AssertionError("must not resolve")))
 value=job();value["input"]={"argv":"arbitrary"}
 assert execute_mautic_version(value,config=None)["status"]=="failed"

def test_version_unknown_uid_does_not_launch(monkeypatch):
 monkeypatch.setattr(core,"resolve_portal_command_uid",lambda *a,**kw:"unknown-uid")
 def missing(*a):raise ValueError("unknown")
 monkeypatch.setattr(cli,"_resolve_registered_command_runtime",missing)
 monkeypatch.setattr(cli,"_run_manual_command_with_scheduler",lambda **kw:(_ for _ in ()).throw(AssertionError("must not launch")))
 assert execute_mautic_version(job(),config=None)["status"]=="failed"

def test_version_capability_has_empty_input_contract():
 cap=mautic_version_capability();assert cap["capability_id"]=="mautic" and cap["commands"][0]["mutates_state"] is False
