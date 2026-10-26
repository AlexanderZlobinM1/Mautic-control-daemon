import os, stat
from urllib.error import HTTPError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.config import load_config
from mcd_agent.mcd_control import ControlStore
from mcd_agent.result_signing import ensure_result_key, sign_result
from mcd_agent.state_push import MCCStatePusher, _ingest_control_response, state_identity_payload

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


def test_control_ack_is_persisted_without_command_envelope_contract_version(tmp_path):
    db_path = str(Path(tmp_path) / "state.db")
    payload = {
        "agent_id": "mcd-agent-1",
        "host_fingerprint": "sha256:" + "a" * 64,
        "result_signing_key": {"key_id": "k1"},
    }
    ack = {
        "key_id": "k1",
        "status": "active",
        "agent_id": "mcd-agent-1",
        "host_fingerprint": payload["host_fingerprint"],
    }

    _ingest_control_response(
        db_path,
        payload,
        {"schema": "mcd-control-v1", "commands": [], "result_signing_key_ack": ack},
    )

    assert ControlStore(db_path).active_key_ack("k1") == ack


def test_control_ack_is_unwrapped_from_mcc_state_response(tmp_path):
    db_path = str(Path(tmp_path) / "state.db")
    payload = {
        "agent_id": "mcd-agent-1",
        "host_fingerprint": "sha256:" + "a" * 64,
        "result_signing_key": {"key_id": "k1"},
    }
    ack = {"key_id": "k1", "status": "active"}

    _ingest_control_response(
        db_path,
        payload,
        {"status": "ok", "control": {"schema": "mcd-control-v1", "commands": [], "result_signing_key_ack": ack}},
    )

    assert ControlStore(db_path).active_key_ack("k1") == ack


def test_state_push_send_persists_wrapped_mcc_ack(tmp_path):
    db_path = str(Path(tmp_path) / "state.db")
    payload = {
        "schema": "mcd-state-v1",
        "agent_id": "mcd-agent-1",
        "host_fingerprint": "sha256:" + "a" * 64,
        "result_signing_key": {"key_id": "k1"},
    }
    body = {
        "status": "ok",
        "control": {
            "schema": "mcd-control-v1",
            "commands": [],
            "result_signing_key_ack": {"key_id": "k1", "status": "active"},
        },
    }

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            import json

            return json.dumps(body).encode("utf-8")

    cfg = SimpleNamespace(
        mcc_push_enabled=True,
        mcc_url="https://mcc.example.test",
        mcc_token="token",
        state_db_path=db_path,
    )
    with patch("mcd_agent.state_push.request.urlopen", return_value=Response()):
        with patch("mcd_agent.state_push.mark_state_snapshot_push_result_mysql"):
            assert MCCStatePusher(cfg).send(payload)[0] is True

    assert ControlStore(db_path).active_key_ack("k1") == {"key_id": "k1", "status": "active"}


def test_state_push_executes_new_control_command_immediately(tmp_path):
    db_path = str(Path(tmp_path) / "state.db")
    key_path = str(Path(tmp_path) / "result.key")
    _private, public = ensure_result_key(key_path)
    command = {
        "command": "docker_runtime_preflight_v1",
        "job_id": "job-immediate",
        "request_id": "request-immediate",
        "probe_id": "probe-immediate",
        "agent_id": "mcd-agent-1",
        "host_fingerprint": "sha256:" + "a" * 64,
        "image_ref": "playground7",
        "image_digest": "sha256:" + "b" * 64,
        "adapter": "mauticrfp-docker-v1",
        "ttl_seconds": 300,
    }
    body = {
        "status": "ok",
        "control": {
            "schema": "mcd-control-v1",
            "contract_version": "1.0",
            "commands": [command],
            "result_signing_key_ack": {
                "key_id": public["key_id"],
                "status": "active",
                "agent_id": command["agent_id"],
                "host_fingerprint": command["host_fingerprint"],
            },
        },
    }

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            import json

            return json.dumps(body).encode("utf-8")

    cfg = SimpleNamespace(
        mcc_push_enabled=True,
        mcc_url="https://mcc.example.test",
        mcc_token="token",
        state_db_path=db_path,
        result_signing_key_path=key_path,
    )
    with patch("mcd_agent.state_push.request.urlopen", return_value=Response()):
        with patch("mcd_agent.state_push.mark_state_snapshot_push_result_mysql"):
            with patch(
                "mcd_agent.state_push.DockerRuntimePreflightRunner",
                return_value=lambda request: {"status": "ready", "image_digest": request["image_digest"]},
            ):
                pusher = MCCStatePusher(cfg)
                assert pusher.send(
                    {
                        "schema": "mcd-state-v1",
                        "agent_id": command["agent_id"],
                        "host_fingerprint": command["host_fingerprint"],
                        "result_signing_key": public,
                    }
                )[0] is True

    receipt = ControlStore(db_path).receipts()[0]
    assert receipt["job_id"] == command["job_id"]
    assert receipt["status"] == "succeeded"
    assert pusher._control_push_pending is True


def test_state_push_reconciles_mcc_immutable_terminal_conflict(tmp_path):
    db_path = str(Path(tmp_path) / "state.db")
    store = ControlStore(db_path)
    command = {"command":"docker_runtime_preflight_v1","job_id":"docker-preflight-worker02-15228","request_id":"r","probe_id":"p","agent_id":"a","host_fingerprint":"sha256:h","image_ref":"playground7","runtime_image_digest":"sha256:"+"a"*64,"adapter":"mauticrfp-docker-v1","ttl_seconds":60}
    store.ingest({"schema":"mcd-control-v1","contract_version":"1.0","commands":[command]}, agent_id="a", host_fingerprint="sha256:h")
    store.execute_pending(agent_id="a", host_fingerprint="sha256:h", active_key=(object(), "k"), runner=lambda _request: {"status":"ready"}, signer=lambda value, *_args: value)
    cfg = SimpleNamespace(mcc_push_enabled=True, mcc_url="https://mcc.example.test", mcc_token="token", state_db_path=db_path)
    conflict = b'{"detail":"control receipt conflicts with immutable terminal command"}'
    error = HTTPError("https://mcc.example.test", 409, "Conflict", {}, __import__("io").BytesIO(conflict))
    with patch("mcd_agent.state_push.request.urlopen", side_effect=error):
        with patch("mcd_agent.state_push.mark_state_snapshot_push_result_mysql"):
            pusher = MCCStatePusher(cfg)
            pusher._control_push_pending = True
            ok, message = pusher.send({"schema":"mcd-state-v1", "agent_id":"a", "host_fingerprint":"sha256:h"})
    assert ok is False
    assert "http 409" in message
    assert pusher._control_push_pending is False
    assert ControlStore(db_path).receipts() == []
    assert ControlStore(db_path).receipts(include_conflict_acknowledged=True)[0]["status"] == "conflict_acknowledged"
