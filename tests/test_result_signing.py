import os, stat
from mcd_agent.result_signing import ensure_result_key, sign_result

def test_result_key_is_private_and_rotation_identity_is_stable(tmp_path):
    path = tmp_path / "result.key"
    private, public = ensure_result_key(str(path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    _, same = ensure_result_key(str(path))
    assert public == same
    assert sign_result({"schema": "x", "value": 1}, private, public["key_id"])["signature"]["key_id"] == public["key_id"]
