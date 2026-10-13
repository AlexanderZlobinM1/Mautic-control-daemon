from __future__ import annotations

import tempfile
import threading
import time
import unittest
import subprocess
import sys
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

    def test_terminal_root_notification_cannot_hold_ready_root(self) -> None:
        """A terminal priority wake must not become a global refill barrier."""

        class StopMainLoop(BaseException):
            pass

        root_terminal = "/var/www/terminal-priority/public_html"
        root_ready = "/var/www/ready-priority/public_html"
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.db"
            config_path = Path(td) / "mcd.toml"
            config_path.write_text(
                "[profile]\nname = \"custom\"\n"
                "[daemon]\npoll_interval_sec = 1\ndispatch_interval_sec = 0.05\n"
                f"[runtime]\nstate_db_path = \"{state_path}\"\n"
                f"scheduler_pause_flag_path = \"{Path(td) / 'scheduler.pause'}\"\n"
                "scheduler_host_max_parallel = 2\n"
                "scheduler_instance_max_parallel = 2\n"
                "scheduler_emergency_reserved_slots = 0\n"
                "segment_mode = \"farm\"\n"
                "segment_priority_parallel_idle = 0\n"
                "segment_regular_parallel_idle = 1\n"
                "segment_priority_parallel_throttled = 0\n"
                "segment_regular_parallel_throttled = 1\n",
                encoding="utf-8",
            )
            config = load_config(str(config_path), allow_recover_from_mcc=False)
            installs = [
                SimpleNamespace(
                    root=root_terminal,
                    db=object(),
                    instance_uid="terminal-priority",
                    name="terminal-priority",
                    mautic_major=6,
                    mautic_timezone="UTC",
                ),
                SimpleNamespace(
                    root=root_ready,
                    db=object(),
                    instance_uid="ready-priority",
                    name="ready-priority",
                    mautic_major=6,
                    mautic_timezone="UTC",
                ),
            ]
            pause_until = time.time() + 10.0
            seed = TaskStore(str(state_path), config)
            for root, entity_id in ((root_terminal, 101), (root_ready, 201)):
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
                        "mautic_major": 6,
                        "updated_at": time.time(),
                        "cycle_id": state["cycle_id"],
                        "cycle_status": "active",
                        "blocked_ids": [],
                        "sql_managed_ids": [],
                        "static_configured_ids": [],
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
                        "db_pause_until": pause_until,
                        "cluster_cron_allowed": True,
                        "cluster_import_allowed": False,
                        "throttled_active": False,
                        "priority_pending": False,
                        "segment_slot_limit": 1,
                    },
                )
            seed.close()

            notifier_ready = threading.Event()
            ready_launch_seen = threading.Event()
            stop = threading.Event()
            spawned: list[tuple[str, int]] = []
            sync_done = False
            real_set_notifier = daemon._scheduler_set_refill_priority_notifier

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
                _next_pid = 99500

                def __init__(self) -> None:
                    type(self)._next_pid += 1
                    self.pid = type(self)._next_pid

                def poll(self) -> None:
                    return None

                def wait(self) -> int:
                    return 0

            def register_notifier(notifier: object) -> None:
                real_set_notifier(notifier)  # type: ignore[arg-type]
                notifier_ready.set()

            def sync_external(**_kwargs: object) -> dict[str, int]:
                nonlocal sync_done
                if not sync_done:
                    self.assertTrue(notifier_ready.wait(timeout=3.0))
                    changed = TaskStore(str(state_path), config)
                    daemon._segment_cycle_terminal(
                        changed,
                        root=root_terminal,
                        entity_id=101,
                        success=True,
                        note="fixture_terminal",
                        now_ts=time.time(),
                    )
                    ready_context = changed.get_runtime_sync(
                        daemon._segment_dispatch_context_key(root_ready)
                    )
                    ready_context["db_pause_until"] = 0.0
                    changed.put_runtime_sync(
                        daemon._segment_dispatch_context_key(root_ready),
                        ready_context,
                    )
                    changed.close()
                    sync_done = True
                return {"observed": 0, "adopted": 0, "updated": 0, "released": 0}

            def spawn_fixture(**kwargs: object) -> FakeProcess:
                args = list(kwargs.get("args") or [])
                root = str(kwargs.get("root") or "")
                entity_id = int(args[-1])
                spawned.append((root, entity_id))
                if root == root_ready:
                    ready_launch_seen.set()
                return FakeProcess()

            def blocking_dispatch_gate(
                dispatch_installs: list[object],
                *,
                busy_roots: set[str],
                lock: threading.RLock,
            ):
                current = next(
                    inst
                    for inst in dispatch_installs
                    if str(getattr(inst, "root") or "") == root_terminal
                )
                with lock:
                    busy_roots.add(root_terminal)
                try:
                    if not ready_launch_seen.wait(timeout=4.0):
                        raise AssertionError(f"ready root was blocked; spawned={spawned!r}")
                    stop.set()
                    raise StopMainLoop()
                finally:
                    with lock:
                        busy_roots.discard(root_terminal)
                yield current

            real_thread = threading.Thread

            class ControlledThread(real_thread):
                def start(self) -> None:
                    if self.name == "mcd-ordinary-persisted-refill":
                        super().start()

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
                patch.object(daemon, "_is_pid_alive", return_value=True),
                patch.object(daemon, "_sync_external_running_tasks", side_effect=sync_external),
                patch.object(daemon, "_iter_instances_with_root_dispatch_gate", side_effect=blocking_dispatch_gate),
                patch.object(daemon, "_scheduler_set_refill_priority_notifier", side_effect=register_notifier),
                patch.object(
                    daemon,
                    "render_mautic_command",
                    side_effect=lambda **kwargs: ["fixture", "segment", str(kwargs.get("id") or 0)],
                ),
                patch.object(daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args),
                patch.object(daemon, "_spawn_command", side_effect=spawn_fixture),
                patch.object(daemon, "cluster_route_allows", return_value=True),
                patch.object(daemon, "backup_lock_active", return_value=False),
                patch.object(daemon.threading, "Thread", ControlledThread),
            ]
            try:
                with ExitStack() as stack:
                    for item in patches:
                        stack.enter_context(item)
                    with self.assertRaises(StopMainLoop):
                        daemon.run_loop(config, single_cycle=False, _test_stop_event=stop)
            finally:
                stop.set()
                refill_threads = [
                    thread
                    for thread in threading.enumerate()
                    if thread.name == "mcd-ordinary-persisted-refill"
                ]
                for thread in refill_threads:
                    thread.join(timeout=2.0)
                daemon._scheduler_set_refill_priority_notifier(None)

            self.assertEqual(spawned, [(root_ready, 201)])

    def test_mautic_failure_retries_to_terminal_and_immediately_backfills(self) -> None:
        """Prove an application failure releases the ordinary scheduler slot.

        The process sequence is deterministic: the failed segment returns a
        non-zero Mautic-style exit three times, exhausting the inclusive
        segment retry budget. The lifecycle monitor must keep the claim
        through bounded retries, write a red terminal cycle without a leaked
        running/claimed ID, and let another root launch immediately. A
        separate long-lived process is checked in the same monitor pass to
        prove the watchdog does not kill healthy work before its one-hour
        timeout. Native PHP/Mautic captures for this contract are collected
        by the private manyroots harness; this test isolates MCD monitor
        semantics from the application runtime.
        """

        failed_root = "/var/www/native-failing/public_html"
        ready_root = "/var/www/native-backfill/public_html"
        failed_id = 901
        ready_id = 902

        class FakeProcess:
            _next_pid = 70000

            def __init__(self, rc: int) -> None:
                type(self)._next_pid += 1
                self.pid = type(self)._next_pid
                self._rc = rc

            def poll(self) -> int:
                return self._rc

        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.db"
            config_path = Path(td) / "mcd.toml"
            config_path.write_text(
                "[profile]\nname = \"custom\"\n"
                "[daemon]\npoll_interval_sec = 1\ndispatch_interval_sec = 0.05\n"
                f"[runtime]\nstate_db_path = \"{state_path}\"\n"
                f"scheduler_pause_flag_path = \"{Path(td) / 'scheduler.pause'}\"\n"
                "scheduler_host_max_parallel = 2\n"
                "scheduler_instance_max_parallel = 2\n"
                "scheduler_emergency_reserved_slots = 0\n"
                "segment_mode = \"farm\"\n"
                "segment_retry_count = 3\n"
                "task_retry_delay_sec = 0\n"
                "command_timeout_sec = 3600\n"
                "worker_watchdog_sec = 3600\n",
                encoding="utf-8",
            )
            config = replace(
                load_config(str(config_path), allow_recover_from_mcc=False),
                segment_retry_count=3,
                task_retry_delay_sec=0,
                command_timeout_sec=3600,
                worker_watchdog_sec=3600,
                scheduler_host_max_parallel=2,
                scheduler_instance_max_parallel=2,
                scheduler_emergency_reserved_slots=0,
            )
            store = TaskStore(str(state_path), config)
            running: dict[str, daemon.RunningTask] = {}
            popens: dict[str, subprocess.Popen[bytes] | FakeProcess] = {}
            terminal_events: list[tuple[str, bool, str | None]] = []
            wakeup = threading.Event()
            long_process: subprocess.Popen[bytes] | None = None

            for root, entity_id in ((failed_root, failed_id), (ready_root, ready_id)):
                state, _added, started = sync_cycle(
                    None,
                    planned_ids=[entity_id],
                    never_built=[],
                    running_ids=[],
                    now_ts=time.time(),
                    important_ids=[],
                    remaining_ids=[entity_id],
                    lazy_mode=False,
                )
                self.assertTrue(started)
                store.put_runtime_sync(cycle_key(root), state)

            daemon._set_scheduler_distribution_roots(
                [failed_root, ready_root],
                worker_slots=2,
                cursor=0,
            )
            spawn_results: deque[FakeProcess | subprocess.Popen[bytes]] = deque(
                [FakeProcess(7), FakeProcess(7), FakeProcess(7)]
            )

            def spawn_fixture(**_kwargs: object) -> FakeProcess | subprocess.Popen[bytes]:
                nonlocal long_process
                if spawn_results:
                    return spawn_results.popleft()
                long_process = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                )
                return long_process

            def terminal_callback(
                task: daemon.RunningTask,
                success: bool,
                note: str | None,
            ) -> None:
                terminal_events.append((task.root, success, note))
                daemon._segment_terminal_and_wake(store, wakeup, task, success, note)

            try:
                with patch.object(daemon, "_SEGMENT_DISPATCH_WAKEUP", None), patch.object(
                    daemon,
                    "_spawn_command",
                    side_effect=spawn_fixture,
                ):
                    launched = daemon._submit_if_slot(
                        config=config,
                        store=store,
                        running=running,
                        root=failed_root,
                        task_type="segment",
                        entity_id=failed_id,
                        args=["php", "bin/console", "mautic:segments:update", "-i", str(failed_id)],
                        timeout_sec=3600,
                        max_parallel_for_type=2,
                        popens=popens,
                        bypass_repeat_guard=True,
                        bypass_baseline_admission=True,
                    )
                    self.assertTrue(launched)
                    failed_key = daemon._task_key(failed_root, "segment", failed_id)

                    for expected_attempt in (2, 3):
                        daemon._monitor_running(
                            config=config,
                            store=store,
                            running=running,
                            popens=popens,
                            segment_terminal_callback=terminal_callback,
                        )
                        self.assertIn(failed_key, running)
                        self.assertEqual(running[failed_key].attempts, expected_attempt)

                    daemon._monitor_running(
                        config=config,
                        store=store,
                        running=running,
                        popens=popens,
                        segment_terminal_callback=terminal_callback,
                    )
                    self.assertNotIn(failed_key, running)
                    self.assertFalse(store.has_running_task_key(failed_key))
                    self.assertEqual(terminal_events, [(failed_root, False, "mautic_failed")])

                    failed_state = store.get_runtime_sync(cycle_key(failed_root)) or {}
                    self.assertEqual(failed_state.get("status"), "terminal")
                    self.assertNotIn(failed_id, failed_state.get("running", []))
                    self.assertNotIn(failed_id, failed_state.get("claimed", []))

                    backfilled = daemon._submit_if_slot(
                        config=config,
                        store=store,
                        running=running,
                        root=ready_root,
                        task_type="segment",
                        entity_id=ready_id,
                        args=["php", "bin/console", "mautic:segments:update", "-i", str(ready_id)],
                        timeout_sec=3600,
                        max_parallel_for_type=2,
                        popens=popens,
                        bypass_repeat_guard=True,
                        bypass_baseline_admission=True,
                    )
                    self.assertTrue(backfilled)
                    ready_key = daemon._task_key(ready_root, "segment", ready_id)
                    self.assertIn(ready_key, running)

                    daemon._monitor_running(
                        config=config,
                        store=store,
                        running=running,
                        popens=popens,
                        segment_terminal_callback=terminal_callback,
                    )
                    self.assertIn(ready_key, running)
                    self.assertEqual(terminal_events, [(failed_root, False, "mautic_failed")])
            finally:
                if long_process is not None and long_process.poll() is None:
                    long_process.terminate()
                    long_process.wait(timeout=5)
                store.close()
                daemon._set_scheduler_distribution_roots([], worker_slots=1)

    def test_process_simulation_many_ready_roots_fill_shared_capacity_with_slow_planner(self) -> None:
        """Exercise full run_loop admission with finite process workers.

        Twelve ready roots share eleven ordinary host slots, each root owns a
        six-worker local cap, and the first planner root is deliberately slow.
        The persisted refill path must still fill the host, preserve first
        turns across distinct roots, backfill completed workers, and leave
        durable cycle claims terminally accounted for without duplicate IDs.

        This is a deterministic Python process simulation.  Real-Mautic/PHP
        acceptance evidence lives in the private native harness, not here.
        """

        roots = ["/var/www/slow-native/public_html"] + [
            f"/var/www/native-{index}/public_html" for index in range(1, 13)
        ]
        root_to_ids = {root: list(range(index * 1000, index * 1000 + 12)) for index, root in enumerate(roots, 1)}
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.db"
            config_path = Path(td) / "mcd.toml"
            config_path.write_text(
                "[profile]\nname = \"custom\"\n"
                "[daemon]\npoll_interval_sec = 1\ndispatch_interval_sec = 0.03\n"
                f"[runtime]\nstate_db_path = \"{state_path}\"\n"
                f"scheduler_pause_flag_path = \"{Path(td) / 'scheduler.pause'}\"\n"
                "scheduler_host_max_parallel = 12\n"
                "scheduler_instance_max_parallel = 6\n"
                "scheduler_emergency_reserved_slots = 1\n"
                "segment_mode = \"farm\"\n"
                "segment_priority_parallel_idle = 0\n"
                "segment_regular_parallel_idle = 6\n"
                "segment_priority_parallel_throttled = 0\n"
                "segment_regular_parallel_throttled = 6\n",
                encoding="utf-8",
            )
            config = load_config(str(config_path), allow_recover_from_mcc=False)
            installs = [
                SimpleNamespace(
                    root=root,
                    db=SimpleNamespace(root=root),
                    instance_uid=root.rsplit("/", 2)[-2],
                    name=root.rsplit("/", 2)[-2],
                    mautic_major=6,
                    mautic_timezone="UTC",
                )
                for root in roots
            ]
            seed = TaskStore(str(state_path), config)
            for root in roots:
                ids = root_to_ids[root]
                state, _added, _started = sync_cycle(
                    None,
                    planned_ids=ids,
                    never_built=[],
                    running_ids=[],
                    now_ts=time.time(),
                    important_ids=[],
                    remaining_ids=ids,
                    lazy_mode=False,
                )
                seed.put_runtime_sync(cycle_key(root), state)
                seed.put_runtime_sync(
                    daemon._segment_dispatch_context_key(root),
                    {
                        "schema": "mcd-segment-dispatch-context-v1",
                        "root": root,
                        "mautic_major": 6,
                        "updated_at": time.time(),
                        "cycle_id": state["cycle_id"],
                        "cycle_status": "active",
                        "blocked_ids": [],
                        "sql_managed_ids": [],
                        "static_configured_ids": [],
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
                        "segment_slot_limit": 6,
                    },
                )
            seed.close()

            spawned: list[dict[str, object]] = []
            stop = threading.Event()
            stop_after = 1.4
            real_thread = threading.Thread

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
                def __init__(self, cfg: object) -> None:
                    self.root = str(getattr(cfg, "root", "") or "")

                def __getattr__(self, _name: str):
                    def call(*_args: object, **_kwargs: object):
                        if self.root == roots[0]:
                            time.sleep(0.12)
                        return []

                    return call

            class DisabledPusher:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                def enabled(self) -> bool:
                    return False

                def __getattr__(self, _name: str):
                    return lambda *_args, **_kwargs: None

            def spawn_fixture(**kwargs: object) -> subprocess.Popen[bytes]:
                args = list(kwargs.get("args") or [])
                proc = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(0.08)"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                spawned.append(
                    {
                        "root": str(kwargs.get("root") or ""),
                        "entity": int(args[-1]),
                        "start": time.monotonic(),
                        "end": None,
                        "proc": proc,
                    }
                )
                return proc

            def monitor_native(**kwargs: object) -> None:
                running = kwargs["running"]
                popens = kwargs["popens"]
                store = kwargs["store"]
                with daemon._SCHEDULER_RUNTIME_LOCK:
                    for key, task in list(running.items()):
                        proc = popens.get(key)
                        if proc is None or proc.poll() is None:
                            continue
                        if int(getattr(task, "row_id", 0) or 0) > 0:
                            store.finish(int(task.row_id), state="done", rc=0, note="native_fixture_complete")
                        if task.entity_id is not None:
                            daemon._segment_cycle_terminal(
                                store,
                                root=str(task.root),
                                entity_id=int(task.entity_id),
                                success=True,
                                note="native_fixture_complete",
                                now_ts=time.time(),
                            )
                        for record in spawned:
                            if record["proc"] is proc and record["end"] is None:
                                record["end"] = time.monotonic()
                        running.pop(key, None)
                        popens.pop(key, None)

            real_dispatch_gate = daemon._iter_instances_with_root_dispatch_gate

            def slow_main_dispatch(
                dispatch_installs: list[object],
                *,
                busy_roots: set[str],
                lock: threading.RLock,
            ):
                for inst in real_dispatch_gate(dispatch_installs, busy_roots=busy_roots, lock=lock):
                    if str(getattr(inst, "root", "") or "") == roots[0]:
                        time.sleep(0.45)
                    yield inst

            class ControlledThread(real_thread):
                def start(self) -> None:
                    if (
                        self.name in {"mcd-ordinary-persisted-refill", "mcd-canonical-lifecycle-scheduler"}
                        or self.name.startswith("mcd-root-lifecycle-")
                    ):
                        super().start()

            def stop_later() -> None:
                time.sleep(stop_after)
                stop.set()

            stopper = real_thread(target=stop_later, name="native-benchmark-stop", daemon=True)
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
                patch.object(daemon, "_monitor_running", side_effect=monitor_native),
                patch.object(daemon, "_reconcile_running_state", return_value={}),
                patch.object(
                    daemon,
                    "_sync_external_running_tasks",
                    return_value={"observed": 0, "adopted": 0, "updated": 0, "released": 0},
                ),
                patch.object(
                    daemon,
                    "render_mautic_command",
                    side_effect=lambda **kwargs: ["native-fixture", "segment", str(kwargs.get("id") or 0)],
                ),
                patch.object(daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args),
                patch.object(daemon, "_spawn_command", side_effect=spawn_fixture),
                patch.object(daemon, "cluster_route_allows", return_value=True),
                patch.object(daemon, "backup_lock_active", return_value=False),
                patch.object(daemon.threading, "Thread", ControlledThread),
                patch.object(daemon, "_iter_instances_with_root_dispatch_gate", side_effect=slow_main_dispatch),
            ]
            stopper.start()
            try:
                with ExitStack() as stack:
                    for item in patches:
                        stack.enter_context(item)
                    daemon.run_loop(config, single_cycle=False, _test_stop_event=stop)
            finally:
                stop.set()
                for thread in threading.enumerate():
                    if thread.name == "mcd-ordinary-persisted-refill":
                        thread.join(timeout=2.0)
                for record in spawned:
                    proc = record["proc"]
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=2.0)
                    if record["end"] is None:
                        record["end"] = time.monotonic()

            self.assertGreaterEqual(len(spawned), 22)
            events = sorted(
                [(float(record["start"]), 1) for record in spawned]
                + [(float(record["end"]), -1) for record in spawned],
                key=lambda item: (item[0], item[1]),
            )
            live = 0
            max_live = 0
            for _timestamp, delta in events:
                live += delta
                max_live = max(max_live, live)
            self.assertGreaterEqual(max_live, 11)

            first_roots: list[str] = []
            for record in sorted(spawned, key=lambda item: float(item["start"])):
                root = str(record["root"])
                if root not in first_roots:
                    first_roots.append(root)
                if len(first_roots) == 11:
                    break
            self.assertEqual(len(first_roots), 11, first_roots)
            self.assertEqual(
                len({(str(record["root"]), int(record["entity"])) for record in spawned}),
                len(spawned),
            )
            earliest_end = min(float(record["end"]) for record in spawned)
            next_start = min(
                float(record["start"])
                for record in spawned
                if float(record["start"]) > earliest_end
            )
            self.assertLess(next_start - earliest_end, 0.5)

            check = TaskStore(str(state_path), config)
            self.assertTrue(
                any(
                    isinstance(state, dict) and bool(state.get("done"))
                    for root in roots
                    for state in [check.get_runtime_sync(cycle_key(root))]
                )
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
