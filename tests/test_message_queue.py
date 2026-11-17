from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import daemon
from mcd_agent.db import MauticDB
from mcd_agent.message_queue import (
    DEFAULT_INTERVAL_SEC,
    collect_message_queue_snapshot,
    effective_message_queue_setting,
    supports_message_queue,
)


def _instance(*, major: int = 6, uid: str = "demo:/var/www/demo", root: str = "/var/www/demo"):
    return SimpleNamespace(
        mautic_major=major,
        instance_uid=uid,
        root=root,
        name="demo.sales-snap.com",
        primary_domain="demo.sales-snap.com",
        domains=["demo.sales-snap.com"],
        db=SimpleNamespace(table_prefix="mautic_"),
    )


def _config(**overrides):
    values = {
        "message_queue_enabled": False,
        "message_queue_interval_sec": DEFAULT_INTERVAL_SEC,
        "message_queue_instance_settings": {},
        "scheduled_jobs": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@dataclass(frozen=True)
class _FrozenQueueConfig:
    message_queue_instance_settings: dict[str, object]


def test_message_queue_support_is_limited_to_mautic_5_6_7() -> None:
    assert not supports_message_queue(_instance(major=4))
    assert supports_message_queue(_instance(major=5))
    assert supports_message_queue(_instance(major=6))
    assert supports_message_queue(_instance(major=7))
    assert not supports_message_queue(_instance(major=8))


def test_message_queue_defaults_to_disabled_hourly() -> None:
    assert effective_message_queue_setting(_config(), _instance()) == (False, 3600)


def test_per_instance_message_queue_setting_overrides_default() -> None:
    inst = _instance()
    config = _config(
        message_queue_enabled=True,
        message_queue_interval_sec=120,
        message_queue_instance_settings={inst.instance_uid: {"enabled": False, "interval_sec": 900}},
    )

    assert effective_message_queue_setting(config, inst) == (False, 900)


def test_cron_adoption_preserves_enabled_state_and_interval() -> None:
    inst = _instance(root="/var/www/demo/public_html")

    settings, added = daemon._adopt_message_queue_settings(
        _config(),
        [inst],
        {"/var/www/demo/public_html": 300},
    )

    assert settings[inst.instance_uid] == {"enabled": True, "interval_sec": 300}
    assert added == [(inst.instance_uid, True, 300, "cron")]


def test_legacy_scheduled_job_is_adopted_without_enabling_disabled_job() -> None:
    inst = _instance()
    legacy_job = SimpleNamespace(
        enabled=False,
        interval_sec=600,
        command_template="mautic:messages:send",
    )

    settings, added = daemon._adopt_message_queue_settings(
        _config(scheduled_jobs=[legacy_job]),
        [inst],
        {},
    )

    assert settings[inst.instance_uid] == {"enabled": False, "interval_sec": 600}
    assert added == [(inst.instance_uid, False, 600, "legacy_jobs")]


def test_missing_legacy_setting_adopts_disabled_hourly_default() -> None:
    inst = _instance()

    settings, added = daemon._adopt_message_queue_settings(_config(), [inst], {})

    assert settings[inst.instance_uid] == {"enabled": False, "interval_sec": 3600}
    assert added == [(inst.instance_uid, False, 3600, "default")]


def test_startup_adoption_replaces_frozen_runtime_config() -> None:
    original = _FrozenQueueConfig(message_queue_instance_settings={})
    settings = {"demo:/var/www/demo": {"enabled": False, "interval_sec": 3600}}

    updated = daemon._with_message_queue_instance_settings(original, settings)

    assert updated is not original
    assert original.message_queue_instance_settings == {}
    assert updated.message_queue_instance_settings == settings


def test_migration_push_uses_fresh_desired_revision() -> None:
    config = _config()
    inst = _instance(uid="demo.sales-snap.com")
    accepted = {"status": "ok", "desired_state": {"revision": 4}}

    with patch.object(
        daemon,
        "fetch_runtime_overrides",
        return_value={"status": "ok", "desired_state": {"revision": 3}},
    ) as fetch, patch.object(
        daemon,
        "push_runtime_overrides",
        return_value=accepted,
    ) as push:
        result = daemon._push_runtime_migration_overrides(
            config,
            [inst],
            {"message_queue_instance_settings": {inst.instance_uid: {"enabled": True}}},
        )

    assert result == accepted
    fetch.assert_called_once_with(config, instance_uids=[inst.instance_uid])
    assert push.call_args.kwargs == {
        "merge": True,
        "target": "desired",
        "desired_state_revision": 3,
    }


def test_migration_push_refetches_once_after_revision_conflict() -> None:
    config = _config()
    inst = _instance(uid="demo.sales-snap.com")

    with patch.object(
        daemon,
        "fetch_runtime_overrides",
        side_effect=[
            {"status": "ok", "desired_state": {"revision": 3}},
            {"status": "ok", "desired_state": {"revision": 4}},
        ],
    ) as fetch, patch.object(
        daemon,
        "push_runtime_overrides",
        side_effect=[
            {"status": "conflict", "reason": "stale_desired_revision"},
            {"status": "ok", "desired_state": {"revision": 5}},
        ],
    ) as push:
        result = daemon._push_runtime_migration_overrides(
            config,
            [inst],
            {"message_queue_instance_settings": {inst.instance_uid: {"enabled": True}}},
        )

    assert result["status"] == "ok"
    assert fetch.call_count == 2
    assert [call.kwargs["desired_state_revision"] for call in push.call_args_list] == [3, 4]


def test_existing_instance_setting_is_never_overwritten_by_cron_adoption() -> None:
    inst = _instance()
    existing = {inst.root: {"enabled": False, "interval_sec": 7200}}

    settings, added = daemon._adopt_message_queue_settings(
        _config(message_queue_instance_settings=existing),
        [inst],
        {inst.root: 60},
    )

    assert settings == existing
    assert added == []


def test_queue_snapshot_exposes_counts_only() -> None:
    inst = _instance()
    database_result = {
        "total": 9,
        "due": 4,
        "exhausted": 2,
        "future": 3,
        "next_scheduled_at": "2026-08-14T14:00:00+00:00",
        "error": "",
        "recipient": "must-not-leak@example.com",
        "payload": "must-not-leak",
    }

    with patch.object(MauticDB, "fetch_message_queue_snapshot", return_value=database_result):
        snapshot = collect_message_queue_snapshot(inst)

    assert snapshot["available"] is True
    assert snapshot["total"] == 9
    assert "recipient" not in snapshot
    assert "payload" not in snapshot


def test_database_snapshot_adapts_to_mautic_queue_columns() -> None:
    queries: list[str] = []

    class Cursor:
        def execute(self, query, params=None):
            queries.append(str(query))

        def fetchone(self):
            if queries[-1].startswith("SHOW TABLES"):
                return {"table": "mautic_message_queue"}
            return {
                "total": 12,
                "due": 7,
                "exhausted": 1,
                "future": 4,
                "next_scheduled_at": None,
            }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def connect():
        yield Connection()

    db = object.__new__(MauticDB)
    db.cfg = SimpleNamespace(table_prefix="mautic_")
    db._connect = connect

    with patch.object(
        MauticDB,
        "_table_columns",
        return_value={"id", "success", "attempts", "max_attempts", "scheduled_date"},
    ):
        snapshot = db.fetch_message_queue_snapshot()

    assert snapshot == {
        "total": 12,
        "due": 7,
        "exhausted": 1,
        "future": 4,
        "next_scheduled_at": None,
        "error": "",
    }
    count_query = queries[-1]
    assert "COALESCE(`success`, 0) = 0" in count_query
    assert "COALESCE(`attempts`, 0) < COALESCE(`max_attempts`, 3)" in count_query
    assert "`scheduled_date` <= UTC_TIMESTAMP()" in count_query
