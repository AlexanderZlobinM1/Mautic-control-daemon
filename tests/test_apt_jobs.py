from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from mcd_agent import cli, daemon
from mcd_agent.apt_jobs import (
    CAPABILITY,
    TERMINAL_PREFIX,
    capability,
    recent_statuses,
    terminal_result,
)


class MemoryStore:
    def __init__(self) -> None:
        self.runtime: dict[str, dict[str, object]] = {}
        self.statuses: dict[int, str] = {}
        self.next_id = 70
        self.enqueued: list[dict[str, object]] = []

    def close(self) -> None:
        return None

    def get_runtime_sync(self, key: str):
        return self.runtime.get(key)

    def put_runtime_sync(self, key: str, value: dict[str, object]) -> None:
        self.runtime[key] = dict(value)

    def list_runtime_sync(self, prefix: str = ""):
        return sorted(
            (key, dict(value))
            for key, value in self.runtime.items()
            if key.startswith(prefix)
        )

    def enqueue_manual_request(self, **kwargs) -> int:
        self.next_id += 1
        self.statuses[self.next_id] = "pending"
        self.enqueued.append(dict(kwargs))
        return self.next_id

    def get_manual_request_status(self, request_id: int):
        return self.statuses.get(request_id)

    def cancel_manual_request(self, request_id: int, note: str = "") -> bool:
        if self.statuses.get(request_id) != "pending":
            return False
        self.statuses[request_id] = "cancelled"
        return True


def _job_args(command: str) -> SimpleNamespace:
    return SimpleNamespace(apt_job_cmd=command, operation_id="apt-job-1")


def test_apt_job_capability_is_explicit() -> None:
    assert CAPABILITY == "mcd-apt-job-v1"
    assert capability() == {
        "schema": "mcd-apt-capabilities-v1",
        "version": "1.0",
        "features": ["mcd-apt-job-v1"],
    }


def test_enqueue_is_durable_idempotent_and_terminal_survives_lost_process(
    tmp_path, monkeypatch, capsys
) -> None:
    store = MemoryStore()
    cfg = SimpleNamespace(
        state_db_path=str(tmp_path / "mcd-state.db"),
        config_file_path=str(tmp_path / "mcd.toml"),
        command_timeout_sec=0,
        mcc_url="https://mcc.invalid",
    )
    monkeypatch.setattr(cli, "TaskStore", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/opt/mcd/venv/bin/mcd-cli")

    assert cli._apt_job_cli(cfg, _job_args("enqueue")) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["status"] == "accepted"
    assert accepted["idempotent_replay"] is False
    assert store.enqueued[0]["task_type"] == "apt_upgrade"
    assert store.enqueued[0]["root"] == str(tmp_path.resolve())

    assert cli._apt_job_cli(cfg, _job_args("enqueue")) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["idempotent_replay"] is True
    assert len(store.enqueued) == 1

    record = next(iter(store.runtime.values()))
    stdout = Path(str(record["stdout_path"]))
    stdout.parent.mkdir(parents=True, exist_ok=True)
    terminal = terminal_result("apt-job-1", {
        "status": "ok",
        "update_rc": 0,
        "upgrade_rc": 0,
        "duration_sec": 17,
    })
    stdout.write_text(TERMINAL_PREFIX + json.dumps(terminal) + "\n", encoding="utf-8")
    store.statuses[int(record["request_id"])] = "lost"

    assert cli._apt_job_cli(cfg, _job_args("status")) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "succeeded"
    assert status["terminal_result"] == terminal
    assert status["notification_delivery_status"] == "pending"
    assert recent_statuses(store, mcc_enabled=True)[0]["status"] == "succeeded"


def test_host_dispatch_keeps_busy_request_pending(tmp_path, monkeypatch) -> None:
    cfg = SimpleNamespace(
        state_db_path=str(tmp_path / "state.db"),
        command_timeout_sec=60,
    )
    command = "\x1f".join([
        "mcd-cli",
        "apt-upgrade",
        "--yes",
        "--machine-result",
        "--operation-id",
        "apt-job-queued",
    ])

    class QueueStore:
        def __init__(self) -> None:
            self.finished: list[tuple[int, str, str]] = []
            self.launched: list[tuple[int, str]] = []

        def pending_manual_requests(self, root: str, limit: int = 32):
            return [{
                "id": 81,
                "task_type": "apt_upgrade",
                "command_str": command,
                "timeout_sec": 0,
            }]

        def finish_manual_request(self, request_id: int, status: str, note: str = "") -> None:
            self.finished.append((request_id, status, note))

        def mark_manual_request_launched(self, request_id: int, task_key: str) -> None:
            self.launched.append((request_id, task_key))

    store = QueueStore()
    monkeypatch.setattr(daemon, "_submit_if_slot", lambda **_kwargs: False)
    daemon._dispatch_host_manual_requests(
        config=cfg,
        store=store,
        running={},
        popens={},
    )
    assert store.finished == []
    assert store.launched == []


def test_apt_upgrade_machine_result_is_terminal(tmp_path, monkeypatch, capsys) -> None:
    cfg = SimpleNamespace()
    monkeypatch.setattr(cli, "load_config", lambda _path: cfg)
    monkeypatch.setattr(cli, "maybe_notify_update", lambda _cfg: None)
    monkeypatch.setattr(cli, "_apt_service_postcheck", lambda: (True, ["services: ok"]))
    monkeypatch.setattr(cli, "_push_state_after_change", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "_configured_external_operation",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "output_events": [],
            "payload": {
                "status": "ok",
                "update_rc": 0,
                "upgrade_rc": 0,
                "duration_sec": 17,
            },
        },
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(sys, "argv", [
        "mcd-cli",
        "apt-upgrade",
        "--config",
        str(tmp_path / "mcd.toml"),
        "--yes",
        "--machine-result",
        "--operation-id",
        "apt-machine-1",
    ])

    assert cli.main() == 0
    lines = capsys.readouterr().out.splitlines()
    encoded = next(line[len(TERMINAL_PREFIX):] for line in lines if line.startswith(TERMINAL_PREFIX))
    receipt = json.loads(encoded)
    assert receipt["schema"] == "mcd-apt-operation-result-v1"
    assert receipt["operation_id"] == "apt-machine-1"
    assert receipt["terminal"] is True
    assert receipt["status"] == "success"
    assert receipt["result"]["update_rc"] == 0
