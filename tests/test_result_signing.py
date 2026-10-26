import os, stat
from pathlib import Path

from mcd_agent.config import load_config
from mcd_agent.result_signing import ensure_result_key, sign_result

def test_result_key_is_private_and_rotation_identity_is_stable(tmp_path):
    path = tmp_path / "result.key"
    private, public = ensure_result_key(str(path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    _, same = ensure_result_key(str(path))
    assert public == same
    assert sign_result({"schema": "x", "value": 1}, private, public["key_id"])["signature"]["key_id"] == public["key_id"]


def test_config_bootstraps_mcd_owned_result_signing_key_path(tmp_path):
    config = Path(tmp_path) / "mcd.toml"
    config.write_text('[profile]\nname = "midi"\n', encoding="utf-8")

    loaded = load_config(str(config), allow_recover_from_mcc=False)

    assert loaded.result_signing_key_path == "/opt/mcd/var/keys/result-signing.key"
