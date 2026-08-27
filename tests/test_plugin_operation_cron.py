from __future__ import annotations

import json
from unittest.mock import patch

from mcd_agent.mode import reconcile_plugin_operation_cron


def test_catalog_cron_is_commented_and_six_hour_interval_is_migrated() -> None:
    crontabs = {
        None: "0 */6 * * * cd /srv/mautic && php bin/console vendor:sync\n",
        "www-data": "",
    }
    writes: dict[str | None, str] = {}

    def read(user=None):
        return 0, crontabs[user]

    def write(content, user=None):
        writes[user] = content
        return 0, ""

    rules = [
        {
            "root": "/srv/mautic",
            "instance_key": "instance-1",
            "operation_key": "vendor:vendor_sync",
            "action": "comment",
            "match": ["bin/console", "vendor:sync"],
            "migrate_schedule": True,
            "interval_field": "interval_sec",
            "enabled_field": "enabled",
        }
    ]
    with (
        patch("mcd_agent.mode.os.geteuid", return_value=0),
        patch("mcd_agent.mode._read_crontab", side_effect=read),
        patch("mcd_agent.mode._write_crontab", side_effect=write),
        patch("mcd_agent.mode._ensure_backup"),
    ):
        result = reconcile_plugin_operation_cron(profile_name="active", install_dir="/opt/mcd", rules=rules)

    assert result.ok
    assert "# 0 */6 * * * cd /srv/mautic && php bin/console vendor:sync" in writes[None]
    migration_lines = [
        line.split("=", 1)[1]
        for line in result.lines
        if line.startswith("MCD_PLUGIN_OPERATION_MIGRATE_JSON=")
    ]
    assert [json.loads(line) for line in migration_lines] == [
        {
            "instance_key": "instance-1",
            "operation_key": "vendor:vendor_sync",
            "cron_found": True,
            "enabled_field": "enabled",
            "interval_field": "interval_sec",
            "interval_sec": 21600,
        }
    ]


def test_unconvertible_catalog_cron_still_enables_operation_without_inventing_interval() -> None:
    crontabs = {
        None: "15 3 * * 1-5 cd /srv/mautic && php bin/console vendor:sync\n",
        "www-data": "",
    }
    rules = [
        {
            "root": "/srv/mautic",
            "instance_key": "instance-1",
            "operation_key": "vendor:vendor_sync",
            "action": "comment",
            "match": ["bin/console", "vendor:sync"],
            "migrate_schedule": True,
            "interval_field": "interval_sec",
            "enabled_field": "enabled",
        }
    ]
    with (
        patch("mcd_agent.mode.os.geteuid", return_value=0),
        patch("mcd_agent.mode._read_crontab", side_effect=lambda user=None: (0, crontabs[user])),
        patch("mcd_agent.mode._write_crontab", return_value=(0, "")),
        patch("mcd_agent.mode._ensure_backup"),
    ):
        result = reconcile_plugin_operation_cron(profile_name="active", install_dir="/opt/mcd", rules=rules)

    migration = json.loads(
        next(line.split("=", 1)[1] for line in result.lines if line.startswith("MCD_PLUGIN_OPERATION_MIGRATE_JSON="))
    )
    assert migration["cron_found"] is True
    assert migration["enabled_field"] == "enabled"
    assert "interval_sec" not in migration
