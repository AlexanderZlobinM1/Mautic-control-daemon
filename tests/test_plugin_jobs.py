from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcd_agent import cli, daemon
from mcd_agent.plugin_jobs import (
    CAPABILITY,
    PluginJobError,
    capability,
    canonical_request,
    recent_statuses,
    terminal_from_output,
)


def test_plugin_job_capability_is_stable() -> None:
    advertised = capability()
    assert CAPABILITY == "mcd-plugin-job-v1"
    assert advertised == {
        "schema": "mcd-plugin-capabilities-v1",
        "version": "1.0",
        "features": [
            "mcd-plugin-job-v1",
            "mcd-plugin-auto-update-policy-v1",
            "mcd-plugin-catalog-digest-v1",
        ],
    }


def test_canonical_request_preserves_ordered_exact_pairs() -> None:
    request = canonical_request(
        instance_uid="instance-a",
        plugin_uids=["vendor.runtime:7", "vendor.companion:7"],
        package_uids=["vendor.runtime.managed:7", "vendor.companion.standard:7"],
        action="install",
        operation_id="plugin-job-1",
    )
    assert request["selections"] == [
        {"plugin_uid": "vendor.runtime:7", "package_uid": "vendor.runtime.managed:7"},
        {"plugin_uid": "vendor.companion:7", "package_uid": "vendor.companion.standard:7"},
    ]
    with pytest.raises(PluginJobError, match="pair_count"):
        canonical_request(
            instance_uid="instance-a",
            plugin_uids=["vendor.runtime:7", "vendor.companion:7"],
            package_uids=["vendor.runtime.managed:7"],
            action="install",
            operation_id="plugin-job-2",
        )


def test_terminal_parser_accepts_only_machine_terminal_result() -> None:
    terminal = {
        "schema": "mcd-plugin-operation-result-v2",
        "terminal": True,
        "status": "success",
        "receipt_sha256": "a" * 64,
    }
    output = "noise\nMCD_PLUGIN_OPERATION_RESULT=" + json.dumps(terminal) + "\n"
    assert terminal_from_output(output) == terminal
    assert terminal_from_output("MCD_PLUGIN_APPLY_RESULT={}") is None


class MemoryStore:
    def __init__(self) -> None:
        self.runtime: dict[str, dict[str, object]] = {}
        self.statuses: dict[int, str] = {}
        self.next_id = 40
        self.enqueued: list[dict[str, object]] = []

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


def _job_args(command: str, *, action: str = "install") -> SimpleNamespace:
    return SimpleNamespace(
        plugin_job_cmd=command,
        operation_id="plugin-job-1",
        instance_uid="instance-a",
        plugin_uid=["vendor.runtime:7"],
        package_uid=["vendor.runtime.managed:7"],
        action=action,
    )


def test_enqueue_is_durable_and_idempotent_and_status_returns_terminal(tmp_path, monkeypatch, capsys) -> None:
    store = MemoryStore()
    cfg = SimpleNamespace(
        state_db_path=str(tmp_path / "mcd-state.db"),
        config_file_path=str(tmp_path / "mcd.toml"),
        command_timeout_sec=0,
        mcc_url=None,
    )
    monkeypatch.setattr(cli, "TaskStore", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(
        cli,
        "_select_instance_for_ops",
        lambda _cfg, selector: SimpleNamespace(instance_uid=selector, root="/srv/instance-a"),
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/opt/mcd/venv/bin/mcd-cli")

    assert cli._plugin_job_cli(cfg, _job_args("enqueue")) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["status"] == "accepted"
    assert accepted["idempotent_replay"] is False
    assert len(store.enqueued) == 1
    assert store.enqueued[0]["task_type"] == "plugins_apply"
    assert int(store.enqueued[0]["timeout_sec"]) == 21600

    assert cli._plugin_job_cli(cfg, _job_args("enqueue")) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["idempotent_replay"] is True
    assert replay["request_id"] == accepted["request_id"]
    assert len(store.enqueued) == 1

    record = next(iter(store.runtime.values()))
    Path(str(record["stdout_path"])).parent.mkdir(parents=True, exist_ok=True)
    terminal = {
        "schema": "mcd-plugin-operation-result-v2",
        "terminal": True,
        "status": "success",
        "receipt_sha256": "b" * 64,
        "inventory_sync_status": "disabled",
    }
    Path(str(record["stdout_path"])).write_text(
        "MCD_PLUGIN_OPERATION_RESULT=" + json.dumps(terminal) + "\n",
        encoding="utf-8",
    )
    store.statuses[int(record["request_id"])] = "done"
    assert cli._plugin_job_cli(cfg, _job_args("status")) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "succeeded"
    assert status["terminal_result"] == terminal
    assert status["notification_delivery_status"] == "disabled"


def test_operation_id_conflict_and_pending_cancel_are_typed(tmp_path, monkeypatch, capsys) -> None:
    store = MemoryStore()
    cfg = SimpleNamespace(
        state_db_path=str(tmp_path / "mcd-state.db"),
        config_file_path=str(tmp_path / "mcd.toml"),
        command_timeout_sec=0,
        mcc_url=None,
    )
    monkeypatch.setattr(cli, "TaskStore", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(
        cli,
        "_select_instance_for_ops",
        lambda _cfg, selector: SimpleNamespace(instance_uid=selector, root="/srv/instance-a"),
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "mcd-cli")
    assert cli._plugin_job_cli(cfg, _job_args("enqueue")) == 0
    capsys.readouterr()
    assert cli._plugin_job_cli(cfg, _job_args("enqueue", action="remove")) == 1
    conflict = json.loads(capsys.readouterr().out)
    assert conflict["reason"] == "plugin_operation_id_conflict"
    assert cli._plugin_job_cli(cfg, _job_args("cancel")) == 0
    cancelled = json.loads(capsys.readouterr().out)
    assert cancelled["status"] == "cancelled"
    assert cancelled["idempotent_replay"] is False
    assert cli._plugin_job_cli(cfg, _job_args("cancel")) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["status"] == "cancelled"
    assert replay["idempotent_replay"] is True


def test_recent_statuses_exposes_bounded_terminal_receipts_without_local_paths(tmp_path) -> None:
    store = MemoryStore()
    stdout = tmp_path / "job.stdout"
    terminal = {
        "schema": "mcd-plugin-operation-result-v2",
        "terminal": True,
        "status": "success",
        "receipt_sha256": "c" * 64,
        "inventory_sync_status": "pending",
    }
    stdout.write_text("MCD_PLUGIN_OPERATION_RESULT=" + json.dumps(terminal) + "\n", encoding="utf-8")
    store.runtime["plugin_apply_job:a"] = {
        "schema": "mcd-plugin-job-record-v1",
        "operation_id": "plugin-job-1",
        "request_id": 41,
        "request_sha256": "d" * 64,
        "stdout_path": str(stdout),
        "stderr_path": str(tmp_path / "job.stderr"),
        "created_at": 20.0,
    }
    store.statuses[41] = "done"

    rows = recent_statuses(store, mcc_enabled=True, limit=1)

    assert len(rows) == 1
    assert rows[0]["schema"] == "mcd-plugin-job-status-result-v1"
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["terminal_result"] == terminal
    assert rows[0]["notification_delivery_status"] == "pending"
    encoded = json.dumps(rows[0], sort_keys=True)
    assert "stdout_path" not in encoded
    assert "stderr_path" not in encoded


def test_busy_manual_worker_leaves_later_plugin_job_queued(monkeypatch, tmp_path) -> None:
    class QueueStore:
        def __init__(self) -> None:
            self.finished: list[tuple[int, str, str]] = []

        def pending_manual_requests(self, _root: str, limit: int = 32):
            return [{
                "id": 42,
                "task_type": "plugins_apply",
                "entity_id": None,
                "command_str": "mcd-cli\x1fplugins\x1f--operation-id\x1fplugin-job-queued",
                "timeout_sec": 21600,
            }]

        def finish_manual_request(self, request_id: int, status: str, note: str = "") -> None:
            self.finished.append((request_id, status, note))

    store = QueueStore()
    monkeypatch.setattr(daemon, "_submit_if_slot", lambda **_kwargs: False)
    daemon._dispatch_manual_requests_for_root(
        config=SimpleNamespace(profile_name="custom", command_timeout_sec=0, state_db_path=str(tmp_path / "state.db")),
        store=store,
        running={"active": SimpleNamespace(root="/srv/instance-a", manual_request_id=41)},
        popens={},
        root="/srv/instance-a",
        seg_sql_ring=daemon.deque(),
        seg_prio_ring=daemon.deque(),
        seg_reg_ring=daemon.deque(),
        trg_prio_ring=daemon.deque(),
        trg_reg_ring=daemon.deque(),
        reb_prio_ring=daemon.deque(),
        reb_reg_ring=daemon.deque(),
    )
    assert store.finished == []
