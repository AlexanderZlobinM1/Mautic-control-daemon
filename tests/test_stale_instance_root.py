from __future__ import annotations

from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcd_agent import daemon
from mcd_agent.inventory import InstanceInventory
from mcd_agent.models import MauticInstall


def _install(root: Path, uid: str, *, source: str = "manual") -> MauticInstall:
    return MauticInstall(
        instance_uid=uid,
        name=uid,
        root=str(root),
        console_path=str(root / "bin" / "console"),
        source=source,
    )


def test_scheduler_excludes_only_missing_host_root(tmp_path: Path) -> None:
    live_root = tmp_path / "live" / "public_html"
    (live_root / "bin").mkdir(parents=True)
    (live_root / "bin" / "console").write_text("#!/usr/bin/env php\n", encoding="utf-8")
    stale = _install(tmp_path / "missing" / "public_html", "stale-instance")
    live = _install(live_root, "live-instance", source="autodiscovery")

    assert daemon._scheduler_runnable_installs([stale, live]) == [live]


def test_scheduler_excludes_inventory_artifact_even_when_root_is_executable(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifact" / "public_html"
    live_root = tmp_path / "live" / "public_html"
    for root in (artifact_root, live_root):
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "console").write_text("#!/usr/bin/env php\n", encoding="utf-8")

    artifact = _install(artifact_root, "artifact-instance", source="artifact")
    # Match the production residue: a host-runtime inventory row may retain a
    # plausible console path. Its artifact source remains authoritative.
    live = _install(live_root, "live-instance", source="autodiscovery")

    assert daemon._scheduler_runnable_installs([artifact, live]) == [live]


def test_artifact_is_excluded_again_at_pending_and_root_dispatch_boundaries(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifact" / "public_html"
    live_root = tmp_path / "live" / "public_html"
    for root in (artifact_root, live_root):
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "console").write_text("#!/usr/bin/env php\n", encoding="utf-8")

    artifact = _install(artifact_root, "gc.sales-snap.com", source="artifact")
    live = _install(live_root, "live-instance", source="autodiscovery")

    pending = daemon._scheduler_pending_roots(
        [artifact, live],
        persisted_pending_roots={str(artifact_root), str(live_root)},
    )
    assert pending == {str(live_root)}

    busy_roots: set[str] = set()
    dispatched = list(
        daemon._iter_instances_with_root_dispatch_gate(
            [artifact, live],
            busy_roots=busy_roots,
            lock=daemon._SCHEDULER_RUNTIME_LOCK,
        )
    )
    assert dispatched == [live]
    assert busy_roots == set()


def test_preserving_manual_inventory_prunes_vanished_root(tmp_path: Path, monkeypatch) -> None:
    live_root = tmp_path / "live" / "public_html"
    (live_root / "bin").mkdir(parents=True)
    (live_root / "bin" / "console").write_text("#!/usr/bin/env php\n", encoding="utf-8")
    stale = _install(tmp_path / "missing" / "public_html", "stale-instance")
    live = _install(live_root, "live-instance")

    inventory = InstanceInventory(str(tmp_path / "state.db"))
    inventory._upsert_install(stale, source="manual")
    inventory._upsert_install(live, source="manual")
    inventory.conn.commit()
    monkeypatch.setattr("mcd_agent.inventory.discover_mautic", lambda *_args, **_kwargs: [])

    discovered = inventory.rescan(
        SimpleNamespace(
            discovery_roots=[],
            exclude_path_contains=[],
            supported_mautic_majors=[4, 5, 6, 7],
            custom_instances=[],
        ),
        preserve_manual=True,
    )

    assert discovered == 0
    assert [(item.instance_uid, item.root) for item in inventory.list_instances()] == [
        ("live-instance", str(live_root))
    ]


def test_missing_console_during_ring_admission_is_nonfatal_and_not_consumed() -> None:
    ring = deque([73])
    config = SimpleNamespace(campaign_rebuild_min_repeat_sec=0, command_timeout_sec=30)

    with patch.object(daemon, "_submit_if_slot") as submit:
        launched = daemon._fill_from_ring(
            ring=ring,
            ring_limit=1,
            total_limit=1,
            root="/var/www/vanished/public_html",
            task_type="campaign_rebuild",
            running={},
            ring_entities={73},
            config=config,
            store=SimpleNamespace(),
            popens={},
            build_args=lambda _entity_id: (_ for _ in ()).throw(
                FileNotFoundError("Console not found in root: /var/www/vanished/public_html")
            ),
            max_launches=1,
        )

    assert launched == 0
    assert list(ring) == [73]
    submit.assert_not_called()


def test_missing_console_does_not_escape_campaign_priority_dispatchers() -> None:
    config = SimpleNamespace(
        php_bin="php",
        mautic_run_as_user="www-data",
        cmd_campaign_trigger_template="mautic:campaigns:trigger -i {id}",
        cmd_campaign_rebuild_template="mautic:campaigns:rebuild -i {id}",
        campaign_limit=0,
        campaign_batch_limit=100,
        campaign_trigger_min_repeat_sec=0,
        campaign_trigger_audit_interval_sec=60,
        campaign_trigger_priority_parallel=1,
        campaign_rebuild_min_repeat_sec=0,
        campaign_rebuild_priority_parallel=1,
    )
    executor = Mock()
    executor.is_active.return_value = False

    with patch.object(
        daemon,
        "render_mautic_command",
        side_effect=FileNotFoundError("Console not found in root: /missing"),
    ):
        triggers = daemon._dispatch_due_campaign_triggers(
            config=config,
            root="/missing",
            due_ids=[73],
            running={},
            priority_executor=executor,
            on_success=Mock(),
        )
        rebuilds = daemon._dispatch_due_campaign_rebuilds(
            config=config,
            root="/missing",
            due_ids=[73],
            running={},
            priority_executor=executor,
            on_success=Mock(),
        )

    assert triggers == []
    assert rebuilds == []
    executor.launch.assert_not_called()
