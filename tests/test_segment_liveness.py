from __future__ import annotations

import tempfile
import unittest
from collections import deque
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import daemon
from mcd_agent.daemon import (
    TaskStore,
    _segment_cycle_admission_inputs,
    _segment_cycle_claim,
    _segment_cycle_pending_order,
    _segment_cycle_pending_ids,
    _segment_cycle_running,
    _segment_cycle_terminal,
    _effective_segment_regular_slot_limit,
    _segment_shared_slots_available,
    _fill_from_ring,
)
from mcd_agent.segment_admission import cycle_key, mark_terminal, sync_cycle


class SegmentLivenessTests(unittest.TestCase):
    """M6/M7 dispatch invariants for persisted ordinary-cycle liveness."""

    @staticmethod
    def _config() -> SimpleNamespace:
        return SimpleNamespace(
            command_timeout_sec=3600,
            segment_full_scan_interval_sec=300,
            scheduler_host_max_parallel=2,
            scheduler_instance_max_parallel=1,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=1,
        )

    def test_recurring_priority_leaves_slot_for_admitted_ordinary_cycle(self) -> None:
        root = "/srv/mautic"
        store = SimpleNamespace(
            get_runtime_sync=lambda key: {"status": "active", "items": [151], "done": [], "claimed": [], "running": []},
            list_runtime_sync=lambda prefix: [],
        )
        config = SimpleNamespace(
            segment_mode="farm",
            segment_priority_parallel_idle=1,
            segment_regular_parallel_idle=1,
            segment_priority_parallel_throttled=1,
            segment_regular_parallel_throttled=1,
        )
        entry = SimpleNamespace(segment_id=58, max_interval_sec=60, instance_uid="instance-1")
        executor = SimpleNamespace()
        with patch.object(daemon, "_segment_shared_slots_available", return_value=1), patch.object(
            daemon, "_segment_cycle_key", return_value="segment-cycle:" + root
        ):
            launched = daemon._dispatch_recurring_priority_segments(
                config=config,
                inst=SimpleNamespace(root=root, instance_uid="instance-1"),
                store=store,
                running={},
                priority_executor=executor,
                entries=[entry],
                enabled=True,
            )
        self.assertEqual(launched, 0)

    def test_full_scan_pending_is_admitted_before_claim_guard(self) -> None:
        pending = {151}
        planned = daemon._reconcile_full_scan_pending(
            pending,
            [151],
            full_scan=False,
            published_ids={151},
        )
        state = {"status": "active", "items": [], "done": [], "errors": [], "claimed": [], "running": []}
        cycle_planned, _ = _segment_cycle_admission_inputs(
            state,
            planned,
            [],
            pressure=False,
        )
        self.assertEqual(cycle_planned, [151])

    def test_pressure_held_candidate_never_enters_orphan_ordinary_ring(self) -> None:
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[153],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        state = mark_terminal(state, entity_id=153, success=True, now_ts=101.0)
        planned, never_built = _segment_cycle_admission_inputs(
            state,
            [154],
            [154],
            pressure=True,
        )
        self.assertEqual(planned, [])
        self.assertEqual(never_built, [])

        next_state, _added, _started = sync_cycle(
            state,
            planned_ids=planned,
            never_built=never_built,
            running_ids=[],
            now_ts=102.0,
        )
        self.assertNotIn(154, next_state["items"])
        self.assertEqual(_segment_cycle_pending_order(next_state), [])

        # A planner candidate that was not durably admitted must not be
        # projected into the ordinary ring merely because it was observed.
        self.assertEqual(
            _segment_cycle_pending_order(
                {
                    "status": "active",
                    "items": [153],
                    "done": [153],
                    "errors": [],
                    "claimed": [],
                    "running": [],
                }
            ),
            [],
        )

    def test_persisted_full_scan_item_bypasses_stale_repeat_cadence(self) -> None:
        root = "/var/www/full-scan/public_html"
        store = TaskStore(":memory:")
        state, _added, _started = sync_cycle(
            None, planned_ids=[151], never_built=[], running_ids=[], now_ts=100.0
        )
        store.put_runtime_sync(cycle_key(root), state)
        with patch.dict(
            daemon._ENTITY_LAUNCH_GUARD,
            {daemon._entity_launch_guard_key(root, "segment", 151): 200.0},
        ), patch.object(daemon, "_spawn_command", return_value=SimpleNamespace(pid=151)), patch.object(
            daemon, "_segment_runtime_marker_write"
        ):
            launched = _fill_from_ring(
                ring=deque([151]), ring_limit=1, total_limit=1, root=root,
                task_type="segment", running={}, ring_entities={151},
                config=self._config(), store=store, popens={},
                build_args=lambda sid: ["php", "segments:update", str(sid)],
                admission_claim=lambda sid: _segment_cycle_claim(
                    store, root=root, entity_id=sid, eligible_ids=[151],
                    blocked_ids=set(), running_ids=set(), now_ts=201.0,
                ),
                bypass_repeat_guard_entities={151}, bypass_baseline_admission=True,
            )
        self.assertEqual(launched, 1)

    def test_super_priority_does_not_consume_regular_slot_for_persisted_pending_item(self) -> None:
        root = "/var/www/farm02/public_html"
        store = TaskStore(":memory:")
        state, _added, _started = sync_cycle(
            None,
            planned_ids=[135, 154],
            never_built=[],
            running_ids=[],
            now_ts=100.0,
        )
        state = mark_terminal(state, entity_id=135, success=True, now_ts=101.0)
        store.put_runtime_sync(cycle_key(root), state)

        config = self._config()
        config.scheduler_host_max_parallel = 0
        config.scheduler_instance_max_parallel = 0
        config.segment_mode = "farm"
        config.segment_priority_parallel_idle = 0
        config.segment_regular_parallel_idle = 1

        class ActiveSuperPriority:
            @staticmethod
            def active_count(active_root: str | None, task_types: set[str]) -> int:
                if active_root == root and task_types == daemon._STATIC_SEGMENT_TASK_TYPES:
                    return 1
                return 0

        running: dict[str, daemon.RunningTask] = {}
        with patch.object(daemon, "_ACTIVE_PRIORITY_EXECUTOR", ActiveSuperPriority()), patch.object(
            daemon, "_spawn_command", return_value=SimpleNamespace(pid=9154)
        ), patch.object(
            daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args
        ), patch.object(
            daemon, "_scheduler_baseline_launch_due", return_value=False
        ), patch.object(daemon, "_SEGMENT_DISPATCH_WAKEUP", None):
            pending_slot_limit = min(
                1,
                _segment_shared_slots_available(
                    running,
                    root,
                    1,
                    store=store,
                    config=config,
                    now_ts=102.0,
                ),
            )
            launched = _fill_from_ring(
                ring=deque([154]),
                ring_limit=pending_slot_limit,
                total_limit=pending_slot_limit,
                root=root,
                task_type="segment",
                running=running,
                ring_entities={154},
                config=config,
                store=store,
                popens={},
                build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
                admission_claim=lambda sid: _segment_cycle_claim(
                    store,
                    root=root,
                    entity_id=sid,
                    eligible_ids=[135, 154],
                    blocked_ids=set(),
                    running_ids=set(),
                    now_ts=102.0,
                ),
                admission_running=lambda sid: _segment_cycle_running(
                    store,
                    root=root,
                    entity_id=sid,
                    now_ts=103.0,
                ),
                admission_release=lambda sid: None,
                bypass_repeat_guard_entities={154},
                bypass_baseline_admission=True,
                max_launches=pending_slot_limit,
            )

        self.assertEqual(pending_slot_limit, 1)
        self.assertEqual(launched, 1)
        self.assertEqual(running[next(iter(running))].entity_id, 154)
        self.assertEqual(store.get_runtime_sync(cycle_key(root))["running"], [154])
        store.close()

    def test_restart_pending_batch_refills_m6_m7_with_independent_super_lane(self) -> None:
        pending_ids = [154, 147, 144, 149, 151]
        for major in (6, 7):
            for throttled in (False, True):
                with self.subTest(mautic_major=major, throttled=throttled), tempfile.TemporaryDirectory() as td:
                    root = f"/var/www/b2bss-m{major}/public_html"
                    store = TaskStore(str(Path(td) / "state.db"))
                    state, _added, started = sync_cycle(
                        None,
                        planned_ids=pending_ids,
                        never_built=[],
                        running_ids=[],
                        now_ts=100.0,
                    )
                    self.assertTrue(started)
                    cycle_id = state["cycle_id"]
                    store.put_runtime_sync(cycle_key(root), state)
                    store.close()
                    store = TaskStore(str(Path(td) / "state.db"))
                    recovered = store.get_runtime_sync(cycle_key(root))
                    self.assertEqual(_segment_cycle_pending_order(recovered), pending_ids)

                    config = self._config()
                    config.scheduler_host_max_parallel = 0
                    config.scheduler_instance_max_parallel = 0
                    config.segment_mode = "farm"
                    config.segment_priority_parallel_idle = 8
                    config.segment_regular_parallel_idle = 3
                    config.segment_priority_parallel_throttled = 2
                    config.segment_regular_parallel_throttled = 0
                    config.segment_throttle_whitelist_only = True
                    config.segment_throttle_whitelist_parallel = 2
                    pending_limit = max(
                        1,
                        _effective_segment_regular_slot_limit(config, throttled),
                    )
                    self.assertEqual(pending_limit, 3 if not throttled else 1)

                    class ActiveSuperPriority:
                        @staticmethod
                        def active_count(active_root: str | None, task_types: set[str]) -> int:
                            if active_root == root and task_types == daemon._STATIC_SEGMENT_TASK_TYPES:
                                return 1
                            return 0

                    running: dict[str, daemon.RunningTask] = {}
                    pids = iter(range(9100 + major * 10 + int(throttled) * 5, 9200))

                    def launch_batch(entity_ids: list[int]) -> int:
                        ring = deque(entity_ids)
                        return _fill_from_ring(
                            ring=ring,
                            ring_limit=pending_limit,
                            total_limit=pending_limit,
                            root=root,
                            task_type="segment",
                            running=running,
                            ring_entities=set(entity_ids),
                            config=config,
                            store=store,
                            popens={},
                            build_args=lambda sid: [
                                "php",
                                "bin/console",
                                "mautic:segments:update",
                                "-i",
                                str(sid),
                            ],
                            admission_claim=lambda sid: _segment_cycle_claim(
                                store,
                                root=root,
                                entity_id=sid,
                                eligible_ids=pending_ids,
                                blocked_ids=set(),
                                running_ids=set(),
                                now_ts=200.0,
                            ),
                            admission_running=lambda sid: _segment_cycle_running(
                                store,
                                root=root,
                                entity_id=sid,
                                now_ts=201.0,
                            ),
                            admission_release=lambda sid: _segment_cycle_release(
                                store,
                                root=root,
                                entity_id=sid,
                                now_ts=202.0,
                            ),
                            bypass_repeat_guard_entities=set(entity_ids),
                            bypass_baseline_admission=True,
                            max_launches=pending_limit,
                        )

                    with patch.object(daemon, "_ACTIVE_PRIORITY_EXECUTOR", ActiveSuperPriority()), patch.object(
                        daemon,
                        "_spawn_command",
                        side_effect=lambda **_kwargs: SimpleNamespace(pid=next(pids)),
                    ) as spawn, patch.object(
                        daemon,
                        "_task_locked_args",
                        side_effect=lambda _key, args, **_kwargs: args,
                    ), patch.object(
                        daemon,
                        "_scheduler_baseline_launch_due",
                        return_value=False,
                    ), patch.object(daemon, "_SEGMENT_DISPATCH_WAKEUP", None):
                        self.assertEqual(
                            _segment_shared_slots_available(
                                running,
                                root,
                                pending_limit,
                                store=store,
                                config=config,
                                now_ts=203.0,
                            ),
                            pending_limit,
                        )
                        first_batch = pending_ids[:pending_limit]
                        self.assertEqual(launch_batch(pending_ids), pending_limit)
                        self.assertEqual(
                            sorted(task.entity_id for task in running.values()),
                            sorted(first_batch),
                        )
                        self.assertEqual(spawn.call_count, pending_limit)
                        self.assertEqual(store.running_count(), pending_limit)
                        running_state = store.get_runtime_sync(cycle_key(root))
                        self.assertEqual(running_state["cycle_id"], cycle_id)
                        self.assertEqual(
                            running_state["running"],
                            first_batch,
                        )

                        for task in list(running.values()):
                            store.finish(task.row_id, state="done", rc=0, note=None)
                            _segment_cycle_terminal(
                                store,
                                root=root,
                                entity_id=int(task.entity_id),
                                success=True,
                                note=None,
                                now_ts=204.0,
                            )
                            running.pop(task.task_key, None)

                        remaining = pending_ids[pending_limit:]
                        completed = list(first_batch)
                        while remaining:
                            batch_size = min(pending_limit, len(remaining))
                            self.assertEqual(launch_batch(remaining), batch_size)
                            self.assertEqual(spawn.call_count, len(completed) + batch_size)
                            self.assertEqual(store.running_count(), batch_size)
                            refill_state = store.get_runtime_sync(cycle_key(root))
                            self.assertEqual(refill_state["cycle_id"], cycle_id)
                            self.assertEqual(refill_state["done"], completed)
                            self.assertEqual(refill_state["running"], remaining[:batch_size])

                            for task in list(running.values()):
                                store.finish(task.row_id, state="done", rc=0, note=None)
                                _segment_cycle_terminal(
                                    store,
                                    root=root,
                                    entity_id=int(task.entity_id),
                                    success=True,
                                    note=None,
                                    now_ts=205.0,
                                )
                                running.pop(task.task_key, None)
                            completed.extend(remaining[:batch_size])
                            remaining = remaining[batch_size:]

                    terminal = store.get_runtime_sync(cycle_key(root))
                    self.assertEqual(terminal["cycle_id"], cycle_id)
                    self.assertEqual(terminal["status"], "terminal")
                    self.assertEqual(terminal["done"], pending_ids)
                    rows = store.conn.execute(
                        "SELECT entity_id, state, pid FROM tasks WHERE root=? ORDER BY id",
                        (root,),
                    ).fetchall()
                    self.assertEqual([int(row["entity_id"]) for row in rows], pending_ids)
                    self.assertTrue(all(str(row["state"]) == "done" for row in rows))
                    self.assertTrue(all(int(row["pid"]) > 0 for row in rows))
                    store.close()

    def _launch_persisted_item(
        self,
        *,
        store: TaskStore,
        root: str,
        entity_id: int,
        eligible_ids: list[int],
        running: dict[str, daemon.RunningTask],
        pid: int,
    ) -> int:
        def claim(sid: int) -> bool:
            return _segment_cycle_claim(
                store,
                root=root,
                entity_id=sid,
                eligible_ids=eligible_ids,
                blocked_ids=set(),
                running_ids=set(),
                now_ts=200.0,
            )

        def mark_running(sid: int) -> None:
            _segment_cycle_running(store, root=root, entity_id=sid, now_ts=201.0)

        return _fill_from_ring(
            ring=deque([entity_id]),
            ring_limit=1,
            total_limit=1,
            root=root,
            task_type="segment",
            running=running,
            ring_entities={entity_id},
            config=self._config(),
            store=store,
            popens={},
            build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
            admission_claim=claim,
            admission_running=mark_running,
            admission_release=lambda sid: None,
            bypass_baseline_admission=True,
            max_launches=1,
        )

    def test_m6_and_m7_persisted_last_claims_starts_and_only_then_next_cycle(self) -> None:
        for major in (6, 7):
            with self.subTest(mautic_major=major), tempfile.TemporaryDirectory() as td:
                root = f"/var/www/m{major}/public_html"
                store = TaskStore(str(Path(td) / "state.db"))
                items = [101, 102, 103]
                state, _added, _started = sync_cycle(
                    None,
                    planned_ids=items,
                    never_built=[],
                    running_ids=[],
                    now_ts=100.0,
                )
                state = mark_terminal(state, entity_id=101, success=True, now_ts=101.0)
                state = mark_terminal(state, entity_id=102, success=True, now_ts=102.0)
                store.put_runtime_sync(cycle_key(root), state)
                store.close()
                store = TaskStore(str(Path(td) / "state.db"))
                recovered = store.get_runtime_sync(cycle_key(root))
                self.assertEqual(_segment_cycle_pending_ids(recovered), {103})
                self.assertEqual(_segment_cycle_pending_order(recovered), [103])
                cycle_id = recovered["cycle_id"]

                running: dict[str, daemon.RunningTask] = {}
                process = SimpleNamespace(pid=7000 + major)
                with patch.object(daemon, "_spawn_command", return_value=process) as spawn, patch.object(
                    daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args
                ), patch.object(daemon, "_scheduler_baseline_launch_due", return_value=False), patch.object(
                    daemon, "_SEGMENT_DISPATCH_WAKEUP", None
                ):
                    launched = self._launch_persisted_item(
                        store=store,
                        root=root,
                        entity_id=103,
                        eligible_ids=items,
                        running=running,
                        pid=process.pid,
                    )

                self.assertEqual(launched, 1)
                spawn.assert_called_once()
                task_row = store.conn.execute(
                    "SELECT state, pid, started_at, entity_id FROM tasks WHERE root=? ORDER BY id DESC LIMIT 1",
                    (root,),
                ).fetchone()
                self.assertIsNotNone(task_row)
                self.assertEqual(task_row["state"], "running")
                self.assertEqual(task_row["pid"], process.pid)
                self.assertGreater(float(task_row["started_at"]), 0.0)
                self.assertEqual(task_row["entity_id"], 103)
                running_state = store.get_runtime_sync(cycle_key(root))
                self.assertEqual(running_state["running"], [103])

                # A terminal callback must complete the old cycle before the
                # next planning observation is allowed to create a new one.
                _segment_cycle_terminal(
                    store,
                    root=root,
                    entity_id=103,
                    success=True,
                    note=None,
                    now_ts=202.0,
                )
                terminal = store.get_runtime_sync(cycle_key(root))
                self.assertEqual(terminal["status"], "terminal")
                self.assertEqual(terminal["done"], items)
                self.assertEqual(terminal["cycle_id"], cycle_id)
                restarted, _added, started_new = sync_cycle(
                    terminal,
                    planned_ids=items,
                    never_built=[],
                    running_ids=[],
                    now_ts=203.0,
                )
                self.assertTrue(started_new)
                self.assertNotEqual(restarted["cycle_id"], terminal["cycle_id"])
                store.close()

    def test_new_admission_still_respects_global_baseline_gate(self) -> None:
        root = "/var/www/new-admission"
        store = TaskStore(":memory:")
        with patch.object(daemon, "_scheduler_baseline_launch_due", return_value=False), patch.object(
            daemon, "_spawn_command"
        ) as spawn:
            launched = daemon._submit_if_slot_uncoordinated(
                config=self._config(),
                store=store,
                running={},
                root=root,
                task_type="segment",
                entity_id=999,
                args=["fixture"],
                timeout_sec=60,
                max_parallel_for_type=1,
                popens={},
                bypass_baseline_admission=False,
            )
        self.assertFalse(launched)
        spawn.assert_not_called()
        store.close()

    def test_campaign_pressure_holds_new_admissions_but_keeps_m6_m7_pending(self) -> None:
        for major in (6, 7):
            with self.subTest(mautic_major=major):
                initial, _added, _started = sync_cycle(
                    None,
                    planned_ids=[11, 12],
                    never_built=[],
                    running_ids=[],
                    now_ts=100.0,
                )
                initial = mark_terminal(initial, entity_id=11, success=True, now_ts=101.0)
                planned, never_built = _segment_cycle_admission_inputs(
                    initial,
                    [12, 13],
                    [13],
                    pressure=True,
                )
                self.assertEqual(planned, [12])
                self.assertEqual(never_built, [])
                held, added, started_new = sync_cycle(
                    deepcopy(initial),
                    planned_ids=planned,
                    never_built=never_built,
                    running_ids=[],
                    now_ts=102.0,
                )
                self.assertFalse(started_new)
                self.assertEqual(added, [])
                self.assertEqual(held["items"], [11, 12])
                self.assertEqual(_segment_cycle_pending_ids(held), {12})

                released_plan, released_never = _segment_cycle_admission_inputs(
                    held,
                    [12, 13],
                    [13],
                    pressure=False,
                )
                released, added, started_new = sync_cycle(
                    held,
                    planned_ids=released_plan,
                    never_built=released_never,
                    running_ids=[],
                    now_ts=103.0,
                )
                self.assertFalse(started_new)
                self.assertEqual(added, [13])
                self.assertEqual(released["items"], [11, 12])
                self.assertEqual(released["follow_up"], [13])

    def test_new_never_built_arrival_uses_follow_up_fifo_after_restart(self) -> None:
        root = "/var/www/b2bss/public_html"
        cycle_admitted_at = 7 * 3600 + 8 * 60 + 17
        new_segment_at = 11 * 3600 + 51 * 60 + 35
        source_before = {
            "3": {"modified_ts": 100.0, "built_ts": 90.0},
            "144": {"modified_ts": 101.0, "built_ts": 91.0},
        }
        source_after = {
            **source_before,
            "154": {"modified_ts": float(new_segment_at), "built_ts": None},
        }
        state, _added, started = sync_cycle(
            None,
            planned_ids=[3, 144],
            never_built=[],
            running_ids=[],
            now_ts=float(cycle_admitted_at),
            source_snapshot=source_before,
        )
        self.assertTrue(started)
        cycle_id = state["cycle_id"]

        state, added, started = sync_cycle(
            state,
            planned_ids=[3, 144, 154],
            never_built=[154],
            running_ids=[],
            now_ts=float(new_segment_at),
            source_snapshot=source_after,
        )
        self.assertFalse(started)
        self.assertEqual(added, [154])
        self.assertEqual(state["cycle_id"], cycle_id)
        self.assertEqual(state["items"], [3, 144])
        self.assertEqual(state["follow_up"], [154])
        self.assertEqual(_segment_cycle_pending_order(state), [154, 3, 144])

        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(str(Path(td) / "state.db"))
            store.put_runtime_sync(cycle_key(root), state)
            store.close()
            store = TaskStore(str(Path(td) / "state.db"))
            recovered = store.get_runtime_sync(cycle_key(root))
            self.assertEqual(_segment_cycle_pending_order(recovered), [154, 3, 144])

            running: dict[str, daemon.RunningTask] = {}
            pending_order = _segment_cycle_pending_order(recovered)
            with patch.object(daemon, "_spawn_command", return_value=SimpleNamespace(pid=9154)) as spawn, patch.object(
                daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args
            ), patch.object(daemon, "_scheduler_baseline_launch_due", return_value=False), patch.object(
                daemon, "_SEGMENT_DISPATCH_WAKEUP", None
            ):
                launched = _fill_from_ring(
                    ring=deque(pending_order),
                    ring_limit=1,
                    total_limit=1,
                    root=root,
                    task_type="segment",
                    running=running,
                    ring_entities=set(pending_order),
                    config=self._config(),
                    store=store,
                    popens={},
                    build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
                    admission_claim=lambda sid: _segment_cycle_claim(
                        store,
                        root=root,
                        entity_id=sid,
                        eligible_ids=pending_order,
                        blocked_ids=set(),
                        running_ids=set(),
                        now_ts=float(new_segment_at),
                    ),
                    admission_running=lambda sid: _segment_cycle_running(
                        store,
                        root=root,
                        entity_id=sid,
                        now_ts=float(new_segment_at + 1),
                    ),
                    admission_release=lambda sid: None,
                    bypass_repeat_guard_entities=set(pending_order),
                    bypass_baseline_admission=True,
                    max_launches=1,
                )

            self.assertEqual(launched, 1)
            spawn.assert_called_once()
            self.assertEqual(running[next(iter(running))].entity_id, 154)
            running_state = store.get_runtime_sync(cycle_key(root))
            self.assertEqual(running_state["items"], [3, 144])
            self.assertEqual(running_state["follow_up"], [154])
            self.assertEqual(running_state["running"], [154])

            _segment_cycle_terminal(
                store,
                root=root,
                entity_id=154,
                success=False,
                note="fixture_failure",
                now_ts=float(new_segment_at + 2),
            )
            failed = store.get_runtime_sync(cycle_key(root))
            self.assertEqual(failed["follow_up"], [154])
            self.assertEqual(failed["errors"], [154])
            self.assertEqual(_segment_cycle_pending_order(failed), [3, 144])
            _segment_cycle_terminal(
                store,
                root=root,
                entity_id=3,
                success=True,
                note=None,
                now_ts=float(new_segment_at + 3),
            )
            _segment_cycle_terminal(
                store,
                root=root,
                entity_id=144,
                success=True,
                note=None,
                now_ts=float(new_segment_at + 4),
            )
            terminal = store.get_runtime_sync(cycle_key(root))
            self.assertEqual(terminal["status"], "terminal")
            retried, _added, started_new = sync_cycle(
                terminal,
                planned_ids=[3, 144, 154],
                never_built=[154],
                running_ids=[],
                now_ts=float(new_segment_at + 5),
            )
            self.assertTrue(started_new)
            self.assertEqual(retried["items"], [3, 144, 154])
            self.assertEqual(retried["follow_up"], [])
            store.close()

    def test_host_wide_free_slots_distribute_persisted_pending_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(str(Path(td) / "state.db"))
            running: dict[str, daemon.RunningTask] = {}
            roots = [f"/var/www/instance-{idx}" for idx in range(3)]
            for index, root in enumerate(roots, start=1):
                state, _added, _started = sync_cycle(
                    None,
                    planned_ids=[index],
                    never_built=[],
                    running_ids=[],
                    now_ts=100.0,
                )
                store.put_runtime_sync(cycle_key(root), state)

            pids = iter([8101, 8102, 8103])
            with patch.object(daemon, "_spawn_command", side_effect=lambda **_kwargs: SimpleNamespace(pid=next(pids))), patch.object(
                daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args
            ), patch.object(daemon, "_scheduler_baseline_launch_due", return_value=False), patch.object(
                daemon, "_SEGMENT_DISPATCH_WAKEUP", None
            ):
                launched = [
                    self._launch_persisted_item(
                        store=store,
                        root=root,
                        entity_id=index,
                        eligible_ids=[index],
                        running=running,
                        pid=8100 + index,
                    )
                    for index, root in enumerate(roots, start=1)
                ]

            self.assertEqual(launched, [1, 1, 1])
            self.assertEqual(len(running), 3)
            self.assertEqual(
                sorted(task.entity_id for task in running.values()),
                [1, 2, 3],
            )
            for root, index in zip(roots, range(1, 4)):
                state = store.get_runtime_sync(cycle_key(root))
                self.assertEqual(state["running"], [index])
            store.close()


if __name__ == "__main__":
    unittest.main()
