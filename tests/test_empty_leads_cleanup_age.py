from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.daemon import (
    _empty_leads_cleanup_effective_setting,
    _migrate_empty_leads_cleanup_runtime,
)
from mcd_agent.db import MauticDB
from mcd_agent.config import load_config
from mcd_agent.models import DBConfig


class _Cursor:
    def __init__(self) -> None:
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.calls.append((str(sql), tuple(params)))
        return 1


class _Connection:
    def __init__(self) -> None:
        self.cursor_value = _Cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cursor_value


def _cleanup_config(**overrides):
    values = {
        "empty_leads_cleanup_enabled": True,
        "empty_leads_cleanup_interval_sec": 900,
        "empty_leads_cleanup_batch_size": 5000,
        "empty_leads_cleanup_min_contact_age_days": 7,
        "empty_leads_cleanup_max_runs_per_window": 0,
        "empty_leads_cleanup_quiet_window_min": 660,
        "empty_leads_cleanup_instance_settings": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_config_default_and_runtime_override_are_seven_and_operator_selected(tmp_path):
    path = tmp_path / "mcd.toml"
    path.write_text("[runtime]\n", encoding="utf-8")
    defaults = load_config(str(path), allow_recover_from_mcc=False)
    assert defaults.empty_leads_cleanup_min_contact_age_days == 7

    path.write_text(
        "[runtime]\nempty_leads_cleanup_min_contact_age_days = 14\n",
        encoding="utf-8",
    )
    selected = load_config(str(path), allow_recover_from_mcc=False)
    assert selected.empty_leads_cleanup_min_contact_age_days == 14


def test_default_age_is_seven_days_and_instance_override_is_preserved():
    inst = SimpleNamespace(instance_uid="instance-a", root="/var/www/a", name="a")
    default = _empty_leads_cleanup_effective_setting(_cleanup_config(), inst)
    assert default[6] == 7

    configured = _cleanup_config(
        empty_leads_cleanup_instance_settings={
            "instance-a": {
                "enabled": True,
                "mode": "both_null",
                "batch_size": 123,
                "min_contact_age_days": 14,
            }
        }
    )
    effective = _empty_leads_cleanup_effective_setting(configured, inst)
    assert effective[5] == 123
    assert effective[6] == 14


def test_legacy_instance_settings_gain_age_without_losing_other_fields():
    config = _cleanup_config(
        config_file_path="/tmp/mcd-cleanup-age-test.toml",
        empty_leads_cleanup_instance_settings={
            "instance-a": {"enabled": False, "mode": "email_null", "batch_size": 11}
        },
    )
    updates = {}
    with patch(
        "mcd_agent.daemon.local_runtime_overrides",
        return_value={"empty_leads_cleanup_instance_settings": config.empty_leads_cleanup_instance_settings},
    ), patch(
        "mcd_agent.daemon.upsert_runtime_values",
        side_effect=lambda _path, values: (updates.update(values) or ("/tmp/config", True)),
    ):
        assert _migrate_empty_leads_cleanup_runtime(config, []) is True

    migrated = updates["empty_leads_cleanup_instance_settings"]["instance-a"]
    assert migrated["enabled"] is False
    assert migrated["mode"] == "email_null"
    assert migrated["batch_size"] == 11
    assert migrated["min_contact_age_days"] == 7
    assert "empty_leads_cleanup_enabled" not in updates
    assert "empty_leads_cleanup_interval_sec" not in updates


def test_delete_sql_excludes_fresh_empty_contacts_with_strict_cutoff():
    db = MauticDB(
        DBConfig(host="localhost", port=3306, name="baza_ss", user="u", password="p", table_prefix="ss_")
    )
    connection = _Connection()
    db._connect = lambda: connection  # type: ignore[method-assign]

    result = db.delete_empty_leads(
        mode="both_null",
        batch_size=5000,
        max_batches=1,
        cutoff_utc="2026-11-20 00:00:00",
    )

    sql, params = connection.cursor_value.calls[0]
    assert "`date_added` < %s" in sql
    assert params == ("2026-11-20 00:00:00", 5000)
    assert result["cutoff_utc"] == "2026-11-20 00:00:00"
