import json,sys
from types import SimpleNamespace
from mcd_agent import cli
from mcd_agent.models import MauticInstall

def setup(monkeypatch):
 row=MauticInstall(instance_uid="one.example.invalid",name="one",root="/registered/root",console_path="/registered/console",runtime_php_bin="/observed/php",runtime_user="observed-user")
 class Inventory:
  def __init__(self,*a):pass
  def list_instances(self):return [row]
 monkeypatch.setattr(cli,"load_config",lambda *a,**kw:SimpleNamespace(state_db_path="unused"))
 monkeypatch.setattr(cli,"maybe_notify_update",lambda *a:None)
 monkeypatch.setattr(cli,"InstanceInventory",Inventory)
 monkeypatch.setattr(cli,"ensure_seeded",lambda *a:None)

def test_existing_json_flag_returns_registered_runtime(monkeypatch,capsys):
 setup(monkeypatch)
 monkeypatch.setattr(sys,"argv",["mcd-cli","instances","list","--instance-uid","one.example.invalid","--json"])
 assert cli.main()==0
 rows=json.loads(capsys.readouterr().out)
 assert len(rows)==1 and rows[0]["runtime_php_bin"]=="/observed/php" and rows[0]["runtime_user"]=="observed-user"

def test_unknown_uid_json_fails_closed(monkeypatch,capsys):
 setup(monkeypatch)
 monkeypatch.setattr(sys,"argv",["mcd-cli","instances","list","--instance-uid","unknown.example.invalid","--json"])
 assert cli.main()==2
 assert json.loads(capsys.readouterr().out)==[]

def test_receipt_bearing_add_registers_before_autodiscovery(monkeypatch,capsys):
 seeded=[]
 added=[]
 class Inventory:
  def __init__(self,*a):pass
  def add_or_update_manual(self,**values):
   added.append(values)
   return {"status":"ok","instance_uid":values["instance_uid"],"root":values["root"],"created":True,"registering_operation_id":values["registering_operation_id"]}
 monkeypatch.setattr(cli,"load_config",lambda *a,**kw:SimpleNamespace(state_db_path="unused"))
 monkeypatch.setattr(cli,"maybe_notify_update",lambda *a:None)
 monkeypatch.setattr(cli,"InstanceInventory",Inventory)
 monkeypatch.setattr(cli,"ensure_seeded",lambda *a:seeded.append(True))
 monkeypatch.setattr(cli,"_push_state_after_change",lambda *a,**kw:None)
 monkeypatch.setattr(sys,"argv",[
  "mcd-cli","instances","add","--name","one.example.invalid",
  "--root","/srv/one","--console-path","/srv/one/bin/console",
  "--instance-uid","one.example.invalid","--runtime-php-bin","/usr/bin/php8.5",
  "--runtime-user","www-data","--operation-id","create-one","--json",
 ])
 assert cli.main()==0
 assert seeded==[]
 assert added[0]["registration_receipt"] is True
 assert added[0]["registering_operation_id"]=="create-one"
 assert json.loads(capsys.readouterr().out)["created"] is True

def test_invalid_json_add_does_not_seed_before_operation_id_validation(monkeypatch,capsys):
 seeded=[]
 class Inventory:
  def __init__(self,*a):pass
  def add_or_update_manual(self,**values):raise ValueError("Registration receipt requires the existing operation ID")
 monkeypatch.setattr(cli,"load_config",lambda *a,**kw:SimpleNamespace(state_db_path="unused"))
 monkeypatch.setattr(cli,"maybe_notify_update",lambda *a:None)
 monkeypatch.setattr(cli,"InstanceInventory",Inventory)
 monkeypatch.setattr(cli,"ensure_seeded",lambda *a:seeded.append(True))
 monkeypatch.setattr(sys,"argv",[
  "mcd-cli","instances","add","--name","one.example.invalid",
  "--root","/srv/one","--console-path","/srv/one/bin/console","--json",
 ])
 assert cli.main()==2
 assert seeded==[]
 assert "Registration receipt requires" in capsys.readouterr().err
