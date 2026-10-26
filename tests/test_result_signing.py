import os, stat
from pathlib import Path
from types import SimpleNamespace

from mcd_agent.config import load_config
from mcd_agent.result_signing import ensure_result_key, sign_result
from mcd_agent.state_push import state_identity_payload

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


def test_config_provides_stable_typed_agent_id_for_state_payload(tmp_path):
    config = Path(tmp_path) / "mcd.toml"
    config.write_text('[mcc]\nhost_name = "worker.example.test"\n', encoding="utf-8")

    first = load_config(str(config), allow_recover_from_mcc=False)
    restarted = load_config(str(config), allow_recover_from_mcc=False)

    assert first.agent_id.startswith("mcd-agent-")
    assert first.agent_id == restarted.agent_id


def test_config_preserves_explicit_mcc_agent_id(tmp_path):
    config = Path(tmp_path) / "mcd.toml"
    config.write_text('[mcc]\nagent_id = "assigned-agent-1"\n', encoding="utf-8")

    loaded = load_config(str(config), allow_recover_from_mcc=False)

    assert loaded.agent_id == "assigned-agent-1"


def test_mcd_state_payload_carries_agent_id_with_host_fingerprint(monkeypatch):
    monkeypatch.setattr("mcd_agent.state_push._host_fingerprint", lambda: "sha256:" + "a" * 64)

    identity = state_identity_payload(SimpleNamespace(agent_id="mcd-agent-1"))
    payload = {"schema": "mcd-state-v1", **identity, "result_signing_key": {"key_id": "k1"}}

    assert payload["schema"] == "mcd-state-v1"
    assert payload["agent_id"] == "mcd-agent-1"
    assert payload["host_fingerprint"] == "sha256:" + "a" * 64
    assert payload["result_signing_key"]["key_id"] == "k1"
