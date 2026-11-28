import json
import sqlite3
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from mcd_agent.config import _remove_retired_result_key_file, load_config
from mcd_agent.mcd_control import ControlStore
from mcd_agent.state_push import MCCStatePusher, state_identity_payload


def test_config_has_no_independent_result_key(tmp_path):
    config = Path(tmp_path) / "mcd.toml"
    retired_key = tmp_path / "retired-result.key"
    retired_key.write_bytes(b"retired-private-key")
    config.write_text(
        '[mcc]\nhost_name = "worker.example.test"\n'
        f'[runtime]\nresult_signing_key_path = "{retired_key}"\n',
        encoding="utf-8",
    )

    loaded = load_config(str(config), allow_recover_from_mcc=False)

    assert not hasattr(loaded, "result_signing_key_path")
    assert loaded.agent_id.startswith("mcd-agent-")
    assert not retired_key.exists()
    assert "result_signing_key_path" not in config.read_text(encoding="utf-8")

    restarted = load_config(str(config), allow_recover_from_mcc=False)
    assert restarted.agent_id == loaded.agent_id


def test_config_preserves_explicit_agent_id(tmp_path):
    config = Path(tmp_path) / "mcd.toml"
    config.write_text('[mcc]\nagent_id = "assigned-agent-1"\n', encoding="utf-8")

    loaded = load_config(str(config), allow_recover_from_mcc=False)

    assert loaded.agent_id == "assigned-agent-1"


def test_retired_result_key_file_is_removed(tmp_path):
    key_path = tmp_path / "result-signing.key"
    key_path.write_bytes(b"retired-private-key")
    key_path.chmod(0o600)

    assert _remove_retired_result_key_file(key_path) is True
    assert not key_path.exists()


def test_state_identity_uses_existing_agent_and_host_fields(monkeypatch):
    monkeypatch.setattr("mcd_agent.state_push._host_fingerprint", lambda: "sha256:" + "a" * 64)

    identity = state_identity_payload(SimpleNamespace(agent_id="mcd-agent-1"))

    assert identity == {
        "agent_id": "mcd-agent-1",
        "host_fingerprint": "sha256:" + "a" * 64,
    }


def test_control_store_removes_legacy_key_state_and_runs_by_job_identity(tmp_path):
    db_path = str(Path(tmp_path) / "state.db")
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE mcd_result_key_ack "
            "(key_id TEXT PRIMARY KEY, ack_json TEXT NOT NULL, updated_at REAL NOT NULL)"
        )

    store = ControlStore(db_path)
    command = {
        "command": "docker_runtime_preflight_v1",
        "job_id": "job-1",
        "request_id": "request-1",
        "probe_id": "probe-1",
        "agent_id": "agent-1",
        "host_fingerprint": "sha256:host",
    }
    store.ingest(
        {"schema": "mcd-control-v1", "contract_version": "1.0", "commands": [command]},
        agent_id="agent-1",
        host_fingerprint="sha256:host",
    )
    receipts = store.execute_pending(
        runner=lambda request: {"request_id": request["request_id"]},
    )

    assert receipts[0]["status"] == "succeeded"
    assert "signature" not in receipts[0]
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mcd_result_key_ack'"
        ).fetchone() is None


def test_existing_control_receipt_loses_retired_signature(tmp_path):
    state_db = tmp_path / "state.sqlite"
    with sqlite3.connect(state_db) as db:
        db.execute(
            "CREATE TABLE mcd_control_jobs "
            "(job_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, "
            "receipt_json TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        db.execute(
            "INSERT INTO mcd_control_jobs VALUES (?,?,?,?)",
            (
                "job-1",
                json.dumps({"job_id": "job-1"}),
                json.dumps(
                    {
                        "job_id": "job-1",
                        "status": "succeeded",
                        "signature": {"value": "retired"},
                    }
                ),
                1.0,
            ),
        )
        db.commit()

    receipt = ControlStore(str(state_db)).receipts()[0]

    assert receipt == {"job_id": "job-1", "status": "succeeded"}


def test_state_push_executes_new_control_command_immediately(tmp_path):
    db_path = str(Path(tmp_path) / "state.db")
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
        },
    }

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            return json.dumps(body).encode("utf-8")

    cfg = SimpleNamespace(
        mcc_push_enabled=True,
        mcc_url="https://mcc.example.test",
        mcc_token="token",
        state_db_path=db_path,
    )
    with patch("mcd_agent.state_push.request.urlopen", return_value=Response()):
        with patch("mcd_agent.state_push.mark_state_snapshot_push_result_mysql"):
            with patch(
                "mcd_agent.state_push.DockerRuntimePreflightRunner",
                return_value=lambda request: {
                    "status": "ready",
                    "image_digest": request["image_digest"],
                },
            ):
                pusher = MCCStatePusher(cfg)
                assert pusher.send(
                    {
                        "schema": "mcd-state-v1",
                        "agent_id": command["agent_id"],
                        "host_fingerprint": command["host_fingerprint"],
                    }
                )[0] is True

    receipt = ControlStore(db_path).receipts()[0]
    assert receipt["job_id"] == command["job_id"]
    assert receipt["status"] == "succeeded"
    assert "signature" not in receipt
    assert pusher._control_push_pending is True


def _terminal_store(db_path: str) -> ControlStore:
    store = ControlStore(db_path)
    command = {
        "command": "docker_runtime_preflight_v1",
        "job_id": "docker-preflight-worker02-15228",
        "request_id": "r",
        "probe_id": "p",
        "agent_id": "a",
        "host_fingerprint": "sha256:h",
        "image_ref": "playground7",
        "runtime_image_digest": "sha256:" + "a" * 64,
        "adapter": "mauticrfp-docker-v1",
        "ttl_seconds": 60,
    }
    store.ingest(
        {"schema": "mcd-control-v1", "contract_version": "1.0", "commands": [command]},
        agent_id="a",
        host_fingerprint="sha256:h",
    )
    store.execute_pending(
        runner=lambda _request: {"status": "ready"},
    )
    return store


def _conflict_error(body: bytes) -> HTTPError:
    return HTTPError(
        "https://mcc.example.test",
        409,
        "Conflict",
        {},
        BytesIO(body),
    )


def test_state_push_reconciles_bound_terminal_conflict(tmp_path):
    db_path = str(Path(tmp_path) / "state.db")
    _terminal_store(db_path)
    cfg = SimpleNamespace(
        mcc_push_enabled=True,
        mcc_url="https://mcc.example.test",
        mcc_token="token",
        state_db_path=db_path,
    )
    conflict = _conflict_error(
        b'{"detail":{"code":"immutable_terminal_conflict","job_id":"docker-preflight-worker02-15228","existing_status":"failed"}}'
    )
    with patch("mcd_agent.state_push.request.urlopen", side_effect=conflict):
        with patch("mcd_agent.state_push.mark_state_snapshot_push_result_mysql"):
            pusher = MCCStatePusher(cfg)
            pusher._control_push_pending = True
            ok, message = pusher.send(
                {"schema": "mcd-state-v1", "agent_id": "a", "host_fingerprint": "sha256:h"}
            )

    assert ok is False
    assert "http 409" in message
    assert pusher._control_push_pending is False
    assert ControlStore(db_path).receipts() == []


def test_state_push_keeps_unbound_terminal_conflict_pending(tmp_path):
    db_path = str(Path(tmp_path) / "state.db")
    _terminal_store(db_path)
    cfg = SimpleNamespace(
        mcc_push_enabled=True,
        mcc_url="https://mcc.example.test",
        mcc_token="token",
        state_db_path=db_path,
    )
    conflict = _conflict_error(
        b'{"detail":"control receipt conflicts with immutable terminal command"}'
    )
    with patch("mcd_agent.state_push.request.urlopen", side_effect=conflict):
        with patch("mcd_agent.state_push.mark_state_snapshot_push_result_mysql"):
            pusher = MCCStatePusher(cfg)
            pusher._control_push_pending = True
            pusher.send(
                {"schema": "mcd-state-v1", "agent_id": "a", "host_fingerprint": "sha256:h"}
            )

    assert pusher._control_push_pending is True
    assert ControlStore(db_path).receipts()[0]["status"] == "succeeded"
