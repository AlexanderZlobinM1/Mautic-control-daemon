from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from types import SimpleNamespace

import pytest

from mcd_agent import instance_migrate
from mcd_agent.instance_identity import (
    INSTANCE_STATE_PROFILES,
    INSTANCE_STATE_SCHEMA,
    INSTANCE_STATE_VERSION,
    identity_readback,
    instance_state_path,
    instance_path_names,
    read_instance_state,
    read_instance_identity,
    validate_instance_code,
    write_instance_identity,
    write_instance_state,
)
from mcd_agent.inventory import InstanceInventory
from mcd_agent.mautic_image_install import build_plan
from mcd_agent.models import DBConfig, MauticInstall
from mcd_agent.state_push import (
    _observe_mautic_version_change,
    _read_pending_instance_change_events,
    _wire_instance_identity,
    _wire_instance_state,
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


def test_instance_state_profile_is_versioned_and_round_trips(tmp_path: Path) -> None:
    root = tmp_path / "news.example.com_7k4m2" / "public_html"
    root.mkdir(parents=True)
    for profile in sorted(INSTANCE_STATE_PROFILES - {"cluster"}):
        payload = write_instance_state(
            root,
            profile=profile,
            instance_uid="mcc-instance-001",
            instance_code="7k4m2",
            primary_domain="news.example.com",
            ownership_confirmed=profile == "artifact",
        )
        assert payload["schema"] == INSTANCE_STATE_SCHEMA
        assert payload["version"] == INSTANCE_STATE_VERSION
        assert read_instance_state(root) == payload
    assert instance_state_path(root).is_file()


def test_cluster_state_requires_complete_ownership_evidence(tmp_path: Path) -> None:
    root = tmp_path / "cluster" / "public_html"
    root.mkdir(parents=True)
    with pytest.raises(ValueError, match="cluster instance state is incomplete"):
        write_instance_state(root, profile="cluster", ownership_confirmed=False)
    payload = write_instance_state(root, profile="cluster", ownership_confirmed=True, cluster_id="cluster-01")
    assert read_instance_state(root) == payload


def test_unknown_or_unversioned_local_state_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "unknown" / "public_html"
    root.mkdir(parents=True)
    state_path = instance_state_path(root)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        '{"schema":"mcd-instance-state-profile-v1","version":99,"profile":"unknown",'
        '"ownership_confirmed":false}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="version is unsupported"):
        read_instance_state(root)


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


def test_identity_snapshot_publishes_local_artifact_state_and_invalid_state(tmp_path: Path) -> None:
    root = tmp_path / "news.example.com_7k4m2" / "public_html"
    root.mkdir(parents=True)
    install = MauticInstall(
        instance_uid="mcc-instance-001",
        name="news.example.com",
        root=str(root),
        console_path=str(root / "bin/console"),
        primary_domain="news.example.com",
        domains=["news.example.com"],
        instance_code="7k4m2",
    )
    identity = _wire_instance_identity(install, "MauticFarm-02")
    derived = _wire_instance_state(install, identity)
    assert derived["profile"] == "active"
    assert derived["source"] == "inventory"
    assert derived["status"] == "derived"

    write_instance_state(
        root,
        profile="artifact",
        instance_uid="mcc-instance-001",
        instance_code="7k4m2",
        primary_domain="news.example.com",
        ownership_confirmed=True,
    )
    artifact = _wire_instance_state(install, identity)
    assert artifact["profile"] == "artifact"
    assert artifact["source"] == "local"
    assert artifact["status"] == "confirmed"
    assert artifact["ownership_confirmed"] is True

    instance_state_path(root).write_text(
        '{"schema":"mcd-instance-state-profile-v1","version":99,"profile":"artifact"}',
        encoding="utf-8",
    )
    invalid = _wire_instance_state(install, identity)
    assert invalid["profile"] == "unknown"
    assert invalid["source"] == "local"
    assert invalid["status"] == "invalid"


def test_artifact_state_is_excluded_from_active_inventory_and_survives_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "inventory.db"
    source = tmp_path / "source" / "public_html"
    target = tmp_path / "target" / "public_html"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    write_instance_state(
        source,
        profile="artifact",
        instance_uid="mcc-source-001",
        instance_code="7k4m2",
        primary_domain="source.example.com",
        ownership_confirmed=True,
    )
    inv = InstanceInventory(str(db_path))
    for uid, root, domain, code in (
        ("mcc-source-001", source, "source.example.com", "7k4m2"),
        ("mcc-target-001", target, "target.example.com", "7k4m3"),
    ):
        inv._upsert_install(
            MauticInstall(
                instance_uid=uid,
                name=domain,
                root=str(root),
                console_path=str(root / "bin/console"),
                primary_domain=domain,
                instance_code=code,
            )
        )
    inv.conn.commit()
    assert [item.instance_uid for item in inv.list_instances()] == ["mcc-target-001"]
    assert [item.instance_uid for item in inv.list_artifact_instances()] == ["mcc-source-001"]

    restarted = InstanceInventory(str(db_path))
    assert [item.instance_uid for item in restarted.list_instances()] == ["mcc-target-001"]
    assert [item.instance_uid for item in restarted.list_artifact_instances()] == ["mcc-source-001"]


def test_rescan_keeps_artifact_out_of_active_inventory(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "inventory.db"
    source = tmp_path / "source" / "public_html"
    target = tmp_path / "target" / "public_html"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    write_instance_state(
        source,
        profile="artifact",
        instance_uid="mcc-source-001",
        instance_code="7k4m2",
        primary_domain="source.example.com",
        ownership_confirmed=True,
    )
    source_install = MauticInstall(
        instance_uid="mcc-source-001",
        name="source.example.com",
        root=str(source),
        console_path=str(source / "bin/console"),
        primary_domain="source.example.com",
        instance_code="7k4m2",
    )
    target_install = MauticInstall(
        instance_uid="mcc-target-001",
        name="target.example.com",
        root=str(target),
        console_path=str(target / "bin/console"),
        primary_domain="target.example.com",
        instance_code="7k4m3",
    )
    inv = InstanceInventory(str(db_path))
    inv._upsert_install(source_install)
    inv.conn.commit()
    monkeypatch.setattr("mcd_agent.inventory.discover_mautic", lambda *args, **kwargs: [target_install])
    count = inv.rescan(SimpleNamespace(discovery_roots=[], exclude_path_contains=[], supported_mautic_majors=[7], custom_instances=[]))
    assert count == 1
    assert [item.instance_uid for item in inv.list_instances()] == ["mcc-target-001"]
    assert [item.instance_uid for item in inv.list_artifact_instances()] == ["mcc-source-001"]


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
