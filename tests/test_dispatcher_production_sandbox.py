from __future__ import annotations

import tempfile
import time
import unittest
from collections import deque
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import daemon
from mcd_agent.config import load_config
from mcd_agent.daemon import TaskStore
from mcd_agent.segment_admission import cycle_key, sync_cycle
from mcd_agent.static_segment_lanes import PRIORITY, SUPER_PRIORITY, reconcile_state


class ProductionDispatcherSandboxTests(unittest.TestCase):
    """Exercise the real run_loop admission path with durable fixture state."""

    def test_unavailable_static_graph_skips_only_static_admission(self) -> None:
        root_static = "/var/www/static-graph-unavailable/public_html"
        root_ordinary = "/var/www/independent-ordinary/public_html"
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.db"
            config_path = Path(td) / "mcd.toml"
            config_path.write_text(
                "[profile]\nname = \"custom\"\n"
                "[daemon]\npoll_interval_sec = 1\ndispatch_interval_sec = 0.05\n"
                f"[runtime]\nstate_db_path = \"{state_path}\"\n"
                f"scheduler_pause_flag_path = \"{Path(td) / 'scheduler.pause'}\"\n"
                "scheduler_host_max_parallel = 4\n"
                "scheduler_instance_max_parallel = 4\n"
                "scheduler_emergency_reserved_slots = 0\n"
                "segment_mode = \"farm\"\n"
                "segment_priority_parallel_idle = 1\n"
                "segment_regular_parallel_idle = 2\n"
                "segment_priority_parallel_throttled = 1\n"
                "segment_regular_parallel_throttled = 2\n",
                encoding="utf-8",
            )
            config = replace(
                load_config(str(config_path), allow_recover_from_mcc=False),
                segment_priority=[],
                segment_super_priority=[],
                segment_priority_instance_settings={
                    "static-root": {"segments": [58]},
                    "ordinary-root": {"segments": []},
                },
                segment_super_priority_instance_settings={
                    "static-root": {"segments": []},
                    "ordinary-root": {"segments": []},
                },
            )

            installs = [
                SimpleNamespace(
                    root=root_static,
                    db=object(),
                    instance_uid="static-root",
                    name="static-root",
                    mautic_major=7,
                    mautic_timezone="UTC",
                ),
                SimpleNamespace(
                    root=root_ordinary,
                    db=object(),
                    instance_uid="ordinary-root",
                    name="ordinary-root",
                    mautic_major=6,
                    mautic_timezone="UTC",
                ),
            ]

            seed = TaskStore(str(state_path), config)
            for root, entity_id in ((root_static, 101), (root_ordinary, 201)):
                state, _added, _started = sync_cycle(
                    None,
                    planned_ids=[entity_id],
                    never_built=[],
                    running_ids=[],
                    now_ts=time.time(),
                    important_ids=[],
                    remaining_ids=[entity_id],
                    lazy_mode=False,
                )
                seed.put_runtime_sync(cycle_key(root), state)
                seed.put_runtime_sync(
                    daemon._segment_dispatch_context_key(root),
                    {
                        "schema": "mcd-segment-dispatch-context-v1",
                        "root": root,
                        "mautic_major": 7 if root == root_static else 6,
                        "updated_at": time.time(),
                        "cycle_id": state["cycle_id"],
                        "cycle_status": "active",
                        "blocked_ids": [],
                        "sql_managed_ids": [],
                        "static_configured_ids": [58] if root == root_static else [],
                        "static_active_ids": [],
                        "active_import_target_ids": [],
                        "parents_by_child": {},
                        "children_by_parent": {},
                        "dependency_snapshot_authoritative": True,
                        "dependency_snapshot_mode": "live_read",
                        "dependency_snapshot_verified": True,
                        "dependency_snapshot_updated_at": time.time(),
                        "scheduler_pause": False,
                        "backup_pause": False,
                        "dispatch_pause": False,
                        "db_pause_until": 0.0,
                        "cluster_cron_allowed": True,
                        "cluster_import_allowed": False,
                        "throttled_active": False,
                        "priority_pending": False,
                        "segment_slot_limit": 2,
                    },
                )

            static_state, _added, _started = reconcile_state(
                None,
                plans={SUPER_PRIORITY: [], PRIORITY: [58]},
                now_ts=time.time(),
            )
            seed.put_runtime_sync(daemon._static_lane_state_key(root_static), static_state)
            seed.close()

            spawned: list[tuple[str, list[str]]] = []

            class FixtureInventory:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                def count(self) -> int:
                    return len(installs)

                def list_instances(self) -> list[object]:
                    return list(installs)

                def rescan(self, *_args: object, **_kwargs: object) -> int:
                    return len(installs)

            class FakeMauticDB:
                def __init__(self, _cfg: object) -> None:
                    pass

                def __getattr__(self, _name: str):
                    return lambda *_args, **_kwargs: []

            class DisabledPusher:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                def enabled(self) -> bool:
                    return False

                def __getattr__(self, _name: str):
                    return lambda *_args, **_kwargs: None

            class FakeProcess:
                _next_pid = 99000

                def __init__(self) -> None:
                    type(self)._next_pid += 1
                    self.pid = type(self)._next_pid

                def poll(self) -> None:
                    return None

                def wait(self) -> int:
                    return 0

            def spawn_fixture(**kwargs: object) -> FakeProcess:
                spawned.append((str(kwargs.get("root") or ""), list(kwargs.get("args") or [])))
                return FakeProcess()

            patches = [
                patch.object(daemon, "InstanceInventory", FixtureInventory),
                patch.object(daemon, "MauticDB", FakeMauticDB),
                patch.object(daemon, "MCCStatePusher", DisabledPusher),
                patch.object(daemon, "_ensure_mcd_service_kill_mode", return_value=False),
                patch.object(daemon, "ensure_nginx_4xx_scan_safety", return_value={"status": "ok"}),
                patch.object(daemon, "ensure_managed_web_firewall_loopback", return_value={"status": "ok"}),
                patch.object(daemon, "fetch_custom_manifest", return_value=([], "fixture")),
                patch.object(daemon, "ensure_seeded", return_value=1),
                patch.object(daemon, "resolve_agent_identity", return_value={"clone_detected": False}),
                patch.object(daemon, "_load_orphans_from_store", return_value={}),
                patch.object(daemon, "_bootstrap_remote_runtime_before_local_baseline", return_value="none"),
                patch.object(daemon, "_initialize_local_runtime_baseline", return_value=({}, "baseline")),
                patch.object(daemon, "_sync_segment_whitelist_file"),
                patch.object(daemon, "_apply_instance_runtime_guard"),
                patch.object(daemon, "_sync_and_publish_form_embed_status", return_value=False),
                patch.object(daemon, "retire_zabbix_mautic_version_userparameter", return_value={"status": "ok"}),
                patch.object(daemon, "_monitor_running"),
                patch.object(daemon, "_reconcile_running_state", return_value={}),
                patch.object(
                    daemon,
                    "_sync_external_running_tasks",
                    return_value={"observed": 0, "adopted": 0, "updated": 0, "released": 0},
                ),
                patch.object(
                    daemon,
                    "render_mautic_command",
                    side_effect=lambda **kwargs: ["fixture", "segment", str(kwargs.get("id") or 0)],
                ),
                patch.object(daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args),
                patch.object(daemon, "_spawn_command", side_effect=spawn_fixture),
                patch.object(daemon, "cluster_route_allows", return_value=True),
                patch.object(daemon, "backup_lock_active", return_value=False),
                patch.object(
                    daemon,
                    "_static_dispatch_dependency_snapshot",
                    return_value=({}, {}, False, "unavailable"),
                ),
            ]
            with ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                daemon.run_loop(config, single_cycle=True)

            self.assertEqual(
                sorted((root, int(args[-1])) for root, args in spawned),
                [(root_ordinary, 201), (root_static, 101)],
            )
            self.assertFalse(any(int(args[-1]) == 58 for _root, args in spawned))

            check = TaskStore(str(state_path), config)
            preserved_static = check.get_runtime_sync(daemon._static_lane_state_key(root_static))
            self.assertEqual(preserved_static["lanes"][PRIORITY]["items"], [58])
            self.assertEqual(preserved_static["lanes"][PRIORITY]["claimed"], [])
            self.assertEqual(
                check.get_runtime_sync(cycle_key(root_static))["running"],
                [101],
            )
            self.assertEqual(
                check.get_runtime_sync(cycle_key(root_ordinary))["running"],
                [201],
            )
            check.close()

    def test_dead_persisted_rows_do_not_consume_real_ordinary_capacity(self) -> None:
        root = "/var/www/dead-taskstore-rows/public_html"
        store = TaskStore(":memory:")
        state, _added, _started = sync_cycle(
            None,
            planned_ids=list(range(200, 211)),
            never_built=[],
            running_ids=[],
            now_ts=100.0,
            remaining_ids=list(range(200, 211)),
            lazy_mode=False,
        )
        store.put_runtime_sync(cycle_key(root), state)
        config = SimpleNamespace(
            command_timeout_sec=3600,
            scheduler_host_max_parallel=12,
            scheduler_instance_max_parallel=12,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=1,
            segment_mode="farm",
            segment_priority_parallel_idle=0,
            segment_regular_parallel_idle=11,
            segment_priority_parallel_throttled=0,
            segment_regular_parallel_throttled=11,
            scheduler_pause_flag_path="/tmp/mcd-test-no-pause",
        )
        for entity_id in range(100, 111):
            stale = daemon.RunningTask(
                row_id=0,
                root=root,
                task_key=daemon._task_key(root, "segment", entity_id),
                task_type="segment",
                entity_id=entity_id,
                command_str=f"php mautic:segments:update {entity_id}",
                timeout_sec=3600,
                attempts=1,
                started_at=1.0,
                pid=70000 + entity_id,
            )
            stale.row_id = store.add_running(stale)

        pids = iter(range(71000, 71011))
        running: dict[str, daemon.RunningTask] = {}
        with patch.object(
            daemon,
            "_is_pid_alive",
            return_value=False,
        ), patch.object(
            daemon,
            "_spawn_command",
            side_effect=lambda **_kwargs: SimpleNamespace(pid=next(pids)),
        ), patch.object(
            daemon,
            "_task_locked_args",
            side_effect=lambda _key, args, **_kwargs: args,
        ), patch.object(
            daemon,
            "_scheduler_baseline_launch_due",
            return_value=False,
        ), patch.object(daemon, "_SEGMENT_DISPATCH_WAKEUP", None):
            launched = daemon._fill_from_ring(
                ring=deque(range(200, 211)),
                ring_limit=11,
                total_limit=11,
                root=root,
                task_type="segment",
                running=running,
                ring_entities=set(range(200, 211)),
                config=config,
                store=store,
                popens={},
                build_args=lambda sid: ["php", "segments:update", str(sid)],
                bypass_repeat_guard_entities=set(range(200, 211)),
                bypass_baseline_admission=True,
                max_launches=11,
            )

        self.assertEqual(launched, 11)
        self.assertEqual(len(running), 11)
        self.assertEqual(len(store.running_rows()), 22)
        store.close()


if __name__ == "__main__":
    unittest.main()
