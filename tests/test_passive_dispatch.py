from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

import mcd_agent.daemon as daemon
from mcd_agent.cli import _run_manual_command_with_scheduler


class _Store:
    def __init__(self) -> None:
        self.finished: list[tuple[int, str, str]] = []

    def pending_manual_requests(self, root: str, limit: int = 32):
        return [
            {
                "id": 17,
                "task_type": "segment",
                "entity_id": 2071,
                "command_str": "php|bin/console|mautic:segments:update",
                "timeout_sec": 1800,
            }
        ]

    def finish_manual_request(self, req_id: int, status: str, message: str) -> None:
        self.finished.append((req_id, status, message))


class _PassivePluginStore(_Store):
    def __init__(self) -> None:
        super().__init__()
        self.launched: list[tuple[int, str]] = []

    def pending_manual_requests(self, root: str, limit: int = 32):
        return [
            {
                "id": 18,
                "task_type": "plugins_apply",
                "entity_id": None,
                "command_str": "mcd-cli\x1fplugins\x1f--machine-result\x1f--operation-id\x1fpassive-plugin-18",
                "timeout_sec": 21600,
            },
            {
                "id": 19,
                "task_type": "segment",
                "entity_id": 2071,
                "command_str": "php|bin/console|mautic:segments:update",
                "timeout_sec": 1800,
            },
        ]

    def mark_manual_request_launched(self, req_id: int, task_key: str) -> None:
        self.launched.append((req_id, task_key))


def _rings() -> dict[str, deque[int]]:
    return {name: deque() for name in (
        "seg_sql_ring",
        "seg_prio_ring",
        "seg_reg_ring",
        "trg_prio_ring",
        "trg_reg_ring",
        "reb_prio_ring",
        "reb_reg_ring",
    )}


def test_passive_dispatch_rejects_manual_user_task() -> None:
    store = _Store()
    rings = _rings()
    config = SimpleNamespace(profile_name="passive", command_timeout_sec=1800)

    with patch.object(daemon, "_submit_if_slot") as submit:
        daemon._dispatch_manual_requests_for_root(
            config=config,
            store=store,
            running={},
            popens={},
            root="/var/www/mautic",
            monitor_cycle_done=None,
            **rings,
        )

    submit.assert_not_called()
    assert store.finished == [(17, "skipped", "passive_profile_user_dispatch_disabled")]


def test_passive_cli_rejects_every_user_task_command() -> None:
    config = SimpleNamespace(profile_name="passive", command_timeout_sec=1800)
    for command in (
        "segments:update",
        "campaign:trigger",
        "campaign:rebuild",
        "campaigns:rebuild",
        "campaigns:update",
        "campaigns:trigger",
        "import",
    ):
        rc, output = _run_manual_command_with_scheduler(
            cfg=config,
            root="/var/www/mautic",
            command=command,
            instance_id=2071,
            php_bin="php",
            timeout_sec=1800,
            run_as_user="www-data",
        )

        assert rc == 2, command
        assert "passive profile rejects MCD user-task execution" in output


def test_passive_dispatch_runs_durable_plugin_jobs_but_rejects_scheduler_tasks(tmp_path) -> None:
    store = _PassivePluginStore()
    rings = _rings()
    config = SimpleNamespace(
        profile_name="passive",
        command_timeout_sec=1800,
        state_db_path=str(tmp_path / "mcd-state.db"),
    )

    with patch.object(daemon, "_submit_if_slot", return_value=True) as submit:
        daemon._dispatch_manual_requests_for_root(
            config=config,
            store=store,
            running={},
            popens={},
            root="/var/www/mautic",
            monitor_cycle_done=None,
            **rings,
        )

    assert store.finished == [(19, "skipped", "passive_profile_user_dispatch_disabled")]
    assert store.launched == [(18, "/var/www/mautic|plugins_apply|-")]
    submit.assert_called_once()
    assert submit.call_args.kwargs["task_type"] == "plugins_apply"
    assert submit.call_args.kwargs["manual_request_id"] == 18
