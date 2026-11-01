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
