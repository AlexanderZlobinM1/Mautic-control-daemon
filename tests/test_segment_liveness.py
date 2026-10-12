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
    _segment_cycle_pending_ids,
    _segment_cycle_running,
    _segment_cycle_terminal,
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
                self.assertEqual(_segment_cycle_pending_ids(state), {103})

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
                self.assertEqual(released["items"], [11, 13, 12])

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
