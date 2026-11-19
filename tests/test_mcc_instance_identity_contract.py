from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mcd_agent import instance_migrate
from mcd_agent.instance_identity import (
    identity_readback,
    instance_path_names,
    read_instance_identity,
    validate_instance_code,
    write_instance_identity,
)
from mcd_agent.inventory import InstanceInventory
from mcd_agent.mautic_image_install import build_plan
from mcd_agent.models import DBConfig, MauticInstall
from mcd_agent.state_push import (
    _observe_mautic_version_change,
    _read_pending_instance_change_events,
    _wire_instance_identity,
    record_mautic_version_change,
)


def test_mcc_code_is_canonical_crockford_and_names_are_not_generated() -> None:
    assert validate_instance_code("7k4m2") == "7k4m2"
    assert instance_path_names("news.example.com", "7k4m2") == {
        "webroot": "/var/www/news.example.com_7k4m2/public_html",
        "db_name": "db_7k4m2",
        "db_user": "usr_7k4m2",
    }
    for value in ("7K4M2", "7k4m", "7l4m2", "7k4m20"):
        with pytest.raises(ValueError):
            validate_instance_code(value)


def test_identity_marker_survives_readback_and_inventory_rescan(tmp_path: Path) -> None:
    root = tmp_path / "news.example.com_7k4m2" / "public_html"
    root.mkdir(parents=True)
    write_instance_identity(
        root,
        instance_uid="mcc-instance-001",
        instance_code="7k4m2",
        primary_domain="news.example.com",
    )
    assert read_instance_identity(root) == {
        "instance_uid": "mcc-instance-001",
        "instance_code": "7k4m2",
        "primary_domain": "news.example.com",
        "root": str(root.resolve()),
    }
    inv = InstanceInventory(str(tmp_path / "inventory.db"))
    inv._upsert_install(
        MauticInstall(
            instance_uid="mcc-instance-001",
            name="news.example.com",
            root=str(root),
            console_path=str(root / "bin/console"),
            primary_domain="news.example.com",
            instance_code="7k4m2",
        )
    )
    inv.conn.commit()
    restored = inv.list_instances()[0]
    assert restored.instance_uid == "mcc-instance-001"
    assert restored.instance_code == "7k4m2"
    inv._upsert_install(
        MauticInstall(
            instance_uid="legacy-rescan-uid",
            name="news.example.com",
            root=str(root),
            console_path=str(root / "bin/console"),
            primary_domain="news.example.com",
        )
    )
    inv.conn.commit()
    restored_after_rescan = inv.list_instances()[0]
    assert restored_after_rescan.instance_uid == "mcc-instance-001"
    assert restored_after_rescan.instance_code == "7k4m2"


def test_identity_snapshot_uses_bare_uid_and_complete_database_tuple() -> None:
    install = MauticInstall(
        instance_uid="mcc-instance-001",
        name="news.example.com",
        root="/var/www/news.example.com_7k4m2/public_html",
        console_path="/var/www/news.example.com_7k4m2/public_html/bin/console",
        instance_code="7k4m2",
        db=DBConfig(
            host="127.0.0.1",
            port=3306,
            name="db_7k4m2",
            user="usr_7k4m2",
            password="secret",
            table_prefix="",
        ),
    )
    assert _wire_instance_identity(install, "MauticFarm-02") == {
        "instance_uid": "mcc-instance-001",
        "instance_code": "7k4m2",
        "database_name": "db_7k4m2",
        "database_user": "usr_7k4m2",
    }
    legacy = install.__class__(
        instance_uid="news.example.com",
        name="news.example.com",
        root="/var/www/news/public_html",
        console_path="/var/www/news/public_html/bin/console",
    )
    assert _wire_instance_identity(legacy, "MauticFarm-02") == {
        "instance_uid": "news.example.com@MauticFarm-02",
    }


def test_new_image_plan_uses_mcc_identity_but_legacy_plan_is_unchanged() -> None:
    managed = build_plan(
        image_ref="default7",
        domain="news.example.com",
        php_version="8.4",
        instance_uid="mcc-instance-001",
        instance_code="7k4m2",
    )
    assert managed.webroot == Path("/var/www/news.example.com_7k4m2/public_html")
    assert managed.db_name == "db_7k4m2"
    assert managed.db_user == "usr_7k4m2"

    legacy = build_plan(image_ref="default7", domain="news.example.com", php_version="8.4")
    assert legacy.webroot == Path("/var/www/news/public_html")
    assert legacy.db_name == "baza_news"
    assert legacy.db_user == "korisnik_news"


def test_migration_with_identity_requires_canonical_domain_code_path(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "www"
    target = root / "news.example.com_7k4m2" / "public_html"
    with (
        monkeypatch.context() as ctx,
    ):
        ctx.setattr(instance_migrate, "MIGRATION_ROOT", root)
        ctx.setattr(instance_migrate, "_canonical_migration_target", lambda _domains, _code=None: target)
        ctx.setattr(instance_migrate, "_target_db_exists", lambda _name: False)
        ctx.setattr(instance_migrate.shutil, "which", lambda _name: "/usr/bin/tool")
        payload = instance_migrate.preflight_target_relay(
            target_root=str(target),
            target_db_name="db_7k4m2",
            domains_json='["news.example.com"]',
            instance_uid="mcc-instance-001",
            instance_code="7k4m2",
        )
    assert payload["ok"] is True
    assert payload["identity"]["instance_uid"] == "mcc-instance-001"
    assert payload["identity"]["instance_code"] == "7k4m2"


def test_version_change_is_event_only_after_actual_change(tmp_path: Path) -> None:
    cfg = SimpleNamespace(
        state_db_path=str(tmp_path / "state.db"),
        state_mysql_backend="sqlite",
        mcc_push_interval_sec=10,
    )
    _observe_mautic_version_change(
        cfg,
        instance_uid="mcc-instance-001",
        root="/var/www/news.example.com_7k4m2/public_html",
        version="7.1.0",
        now_ts=1.0,
    )
    assert _read_pending_instance_change_events(cfg) == []
    _observe_mautic_version_change(
        cfg,
        instance_uid="mcc-instance-001",
        root="/var/www/news.example.com_7k4m2/public_html",
        version="7.1.1",
        now_ts=2.0,
    )
    events = _read_pending_instance_change_events(cfg)
    assert len(events) == 1
    assert events[0]["before"] == "7.1.0"
    assert events[0]["after"] == "7.1.1"
    assert events[0]["actor"] == "external"
    assert events[0]["source"] == "mautic_runtime_sync"
    assert _read_pending_instance_change_events(cfg)[0]["event_id"] == events[0]["event_id"]


def test_known_mcd_version_change_is_not_reclassified_as_external(tmp_path: Path) -> None:
    cfg = SimpleNamespace(
        state_db_path=str(tmp_path / "state.db"),
        state_mysql_backend="sqlite",
        mcc_push_interval_sec=10,
    )
    _observe_mautic_version_change(
        cfg,
        instance_uid="mcc-instance-001",
        root="/var/www/news.example.com_7k4m2/public_html",
        version="7.1.0",
        now_ts=1.0,
    )
    record_mautic_version_change(
        cfg,
        instance_uid="mcc-instance-001",
        root="/var/www/news.example.com_7k4m2/public_html",
        before="7.1.0",
        after="7.1.1",
        actor="mcd",
        source="mautic_upgrade",
        now_ts=2.0,
    )
    events = _read_pending_instance_change_events(cfg)
    assert len(events) == 1
    assert events[0]["actor"] == "mcd"
    assert events[0]["source"] == "mautic_upgrade"
    _observe_mautic_version_change(
        cfg,
        instance_uid="mcc-instance-001",
        root="/var/www/news.example.com_7k4m2/public_html",
        version="7.1.1",
        now_ts=3.0,
    )
    assert len(_read_pending_instance_change_events(cfg)) == 1
