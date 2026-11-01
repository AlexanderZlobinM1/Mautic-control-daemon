"""Shared stable/Dev nullable runtime clearing and local socket preservation."""
import pytest
from mcd_agent import config

@pytest.mark.parametrize("key,value", [("cluster_node_index",1),("backup_cluster_authority_host","qa-backup.invalid"),("mautic6_core_patch_version_min","6.0.0")])
def test_nullable_runtime_clear_round_trip(tmp_path,key,value):
    path=tmp_path/"mcd.toml"
    path.write_text('[profile]\nname="custom"\n[runtime]\n')
    cfg=config.load_config(str(path),allow_recover_from_mcc=False)
    cfg,_,_,_=config.apply_runtime_overrides(cfg,{key:value})
    cfg,applied,unsupported,blocked=config.apply_runtime_overrides(cfg,{key:None})
    assert applied==[key] and not unsupported and not blocked
    assert getattr(cfg,config._RUNTIME_TO_ATTR[key]) is None
    config.upsert_runtime_values(str(path),{key:None})
    restored=config.load_config(str(path),allow_recover_from_mcc=False)
    assert getattr(restored,config._RUNTIME_TO_ATTR[key]) is None
    assert config.runtime_overrides_from_config_file(str(path))[key] is None
    assert config.runtime_effective_map(restored)[key] is None

def test_remote_empty_socket_preserves_working_local_socket(tmp_path):
    path=tmp_path/"mcd.toml"
    path.write_text('[profile]\nname="custom"\n[runtime]\nstate_mysql_unix_socket="/run/mysqld/working.sock"\n')
    cfg=config.load_config(str(path),allow_recover_from_mcc=False)
    result,_,_,_=config.apply_runtime_overrides(cfg,{"state_mysql_unix_socket":None})
    assert result.state_mysql_unix_socket=="/run/mysqld/working.sock"
