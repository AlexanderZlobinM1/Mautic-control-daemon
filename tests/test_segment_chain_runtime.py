from __future__ import annotations

import json
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import daemon
from mcd_agent.daemon import (
    TaskStore,
    _fill_from_ring,
    _segment_cycle_claim,
    _segment_cycle_key,
    _segment_cycle_running,
    _segment_cycle_terminal,
    _segment_dispatch_context_key,
    _segment_terminal_and_wake,
)
from mcd_agent.segment_admission import sync_cycle


class SegmentChainRuntimeTests(unittest.TestCase):
    @staticmethod
    def _config() -> SimpleNamespace:
        return SimpleNamespace(
            command_timeout_sec=30,
            worker_watchdog_sec=0,
            task_retry_max=1,
            segment_full_scan_interval_sec=0,
            scheduler_host_max_parallel=1,
            scheduler_instance_max_parallel=1,
            segment_mode="id_weighted",
            segment_priority_parallel_idle=1,
            segment_regular_parallel_idle=1,
            campaign_total_parallel=1,
            campaign_priority_parallel=1,
            campaign_regular_parallel=0,
            campaign_trigger_priority_parallel=1,
            campaign_trigger_regular_parallel=0,
            campaign_rebuild_priority_parallel=1,
            campaign_rebuild_regular_parallel=0,
        )

    def _run_fixture(
        self,
        *,
        major: int,
        candidate_ids: list[int],
        parents: dict[str, list[int]],
        children: dict[str, list[int]],
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory(prefix="mcd-chain-") as temp_dir:
            root = str(Path(temp_dir) / "root")
            Path(root).mkdir()
            state_path = str(Path(temp_dir) / "state.db")
            output_path = Path(temp_dir) / "execution.log"
            store = TaskStore(state_path)
            now = time.time()
            cycle, _added, _started = sync_cycle(
                None,
                planned_ids=candidate_ids,
                never_built=candidate_ids,
                running_ids=[],
                now_ts=now,
                important_ids=candidate_ids,
                remaining_ids=[],
            )
            store.put_runtime_sync(_segment_cycle_key(root), cycle)
            store.put_runtime_sync(
                _segment_dispatch_context_key(root),
                {
                    "schema": "mcd-segment-dispatch-context-v1",
                    "root": root,
                    "mautic_major": major,
                    "updated_at": now,
                    "parents_by_child": parents,
                    "children_by_parent": children,
                    "native_segment_exclude_supported": major >= 7,
                },
            )
            running: dict[str, daemon.RunningTask] = {}
            popens = {}
            claimed: set[int] = set()
            active_native_roots: set[int] = set()
            blocked_native_attempts = 0
            wakeup = daemon.threading.Event()

            def claim(sid: int) -> bool:
                ok = _segment_cycle_claim(
                    store,
                    root=root,
                    entity_id=int(sid),
                    eligible_ids=set(candidate_ids),
                    blocked_ids=set(),
                    running_ids=claimed,
                    now_ts=time.time(),
                )
                if ok:
                    claimed.add(int(sid))
                return ok

            def mark_running(sid: int) -> None:
                _segment_cycle_running(store, root=root, entity_id=int(sid), now_ts=time.time())

            def mark_terminal(sid: int, success: bool, note: str | None = None) -> None:
                _segment_cycle_terminal(
                    store,
                    root=root,
                    entity_id=int(sid),
                    success=success,
                    note=note,
                    now_ts=time.time(),
                )

            native_overlap_guard = major >= 7 and len(candidate_ids) > 1

            def dynamic_blocked(sid: int) -> bool:
                nonlocal blocked_native_attempts
                if not native_overlap_guard:
                    return False
                candidate_related = daemon.segment_related_ids(
                    int(sid),
                    {int(key): {int(value) for value in values} for key, values in parents.items()},
                    {int(key): {int(value) for value in values} for key, values in children.items()},
                )
                for active_id in active_native_roots:
                    active_related = daemon.segment_related_ids(
                        int(active_id),
                        {int(key): {int(value) for value in values} for key, values in parents.items()},
                        {int(key): {int(value) for value in values} for key, values in children.items()},
                    )
                    if candidate_related & active_related:
                        blocked_native_attempts += 1
                        return True
                return False

            def on_launch(sid: int) -> None:
                if native_overlap_guard:
                    active_native_roots.add(int(sid))

            def terminal_callback(task: daemon.RunningTask, success: bool, note: str | None) -> None:
                if native_overlap_guard and task.entity_id is not None:
                    active_native_roots.discard(int(task.entity_id))
                _segment_terminal_and_wake(store, wakeup, task, success, note)

            ring = deque(candidate_ids)
            config = self._config()

            def build_args(sid: int) -> list[str]:
                if major >= 7:
                    script = (
                        f'if [ "$0" != "--exclude=21" ] && [ "$1" != "21" ]; then printf \'%s\\n\' 21 >> {output_path}; fi; '
                        f"printf '%s\\n' {int(sid)} >> {output_path}; sleep 0.05; "
                        f"true # mautic:segments:update -i {int(sid)}"
                    )
                else:
                    script = (
                        f"printf '%s\\n' {int(sid)} >> {output_path}; sleep 0.05; "
                        f"true # mautic:segments:update -i {int(sid)}"
                    )
                return ["/bin/sh", "-c", script]

            launched = _fill_from_ring(
                ring=ring,
                ring_limit=2 if native_overlap_guard else 1,
                total_limit=2 if native_overlap_guard else 1,
                root=root,
                task_type="segment",
                running=running,
                ring_entities=set(candidate_ids),
                config=config,
                store=store,
                popens=popens,
                build_args=build_args,
                admission_claim=claim,
                admission_running=mark_running,
                admission_release=lambda sid: None,
                on_launch=on_launch,
                dynamic_blocked=dynamic_blocked,
                remove_on_launch=True,
                bypass_baseline_admission=True,
                max_launches=2 if native_overlap_guard else 1,
            )
            self.assertEqual(launched, 1)
            self.assertEqual(len(running), 1)
            task = next(iter(running.values()))
            self.assertGreater(task.pid, 0)
            if major == 6:
                self.assertEqual(
                    task.command_str.count("mautic:segments:update -i"),
                    len(candidate_ids),
                )
            else:
                self.assertEqual(task.command_str.count("mautic:segments:update -i"), 2)

            deadline = time.monotonic() + 10.0
            task_pids = [int(task.pid)]
            task_commands = [task.command_str]
            while (running or ring) and time.monotonic() < deadline:
                if not running and ring:
                    launched += _fill_from_ring(
                        ring=ring,
                        ring_limit=2 if native_overlap_guard else 1,
                        total_limit=2 if native_overlap_guard else 1,
                        root=root,
                        task_type="segment",
                        running=running,
                        ring_entities=set(candidate_ids),
                        config=config,
                        store=store,
                        popens=popens,
                        build_args=build_args,
                        admission_claim=claim,
                        admission_running=mark_running,
                        admission_release=lambda sid: None,
                        on_launch=on_launch,
                        dynamic_blocked=dynamic_blocked,
                        remove_on_launch=True,
                        bypass_baseline_admission=True,
                        max_launches=2 if native_overlap_guard else 1,
                    )
                    task_pids.extend(int(item.pid) for item in running.values())
                    task_commands.extend(item.command_str for item in running.values())
                if not running:
                    time.sleep(0.05)
                    continue
                daemon._monitor_running(
                    config=config,
                    store=store,
                    running=running,
                    popens=popens,
                    segment_terminal_callback=terminal_callback,
                )
                time.sleep(0.05)
            self.assertFalse(running)
            self.assertFalse(ring)
            state = store.get_runtime_sync(_segment_cycle_key(root)) or {}
            lines = output_path.read_text(encoding="utf-8").splitlines()
            receipt = {
                "major": major,
                "candidate_ids": candidate_ids,
                "launched_workers": len(set(task_pids)),
                "task_pid": int(task.pid),
                "task_pids": sorted(set(task_pids)),
                "task_commands": task_commands,
                "task_key": task.task_key,
                "task_command": task.command_str,
                "claimed": sorted(claimed),
                "done": sorted(int(value) for value in state.get("done") or []),
                "errors": sorted(int(value) for value in state.get("errors") or []),
                "execution_order": [int(value) for value in lines],
                "blocked_native_attempts": blocked_native_attempts,
                "native_covered_ids": sorted(daemon._segment_native_covered_ids(store, root)),
            }
            store.close()
            print("SEGMENT_CHAIN_RECEIPT " + json.dumps(receipt, sort_keys=True))
            return receipt

    def test_m6_fork_diamond_uses_one_taskstore_pid_for_whole_closure(self) -> None:
        receipt = self._run_fixture(
            major=6,
            candidate_ids=[21, 22, 11, 57, 106, 61],
            parents={"11": [21, 22], "57": [11], "106": [11], "61": [57, 106]},
            children={"21": [11], "22": [11], "11": [57, 106], "57": [61], "106": [61]},
        )
        self.assertEqual(receipt["launched_workers"], 1)
        self.assertEqual(sorted(receipt["claimed"]), sorted(receipt["candidate_ids"]))
        self.assertEqual(sorted(receipt["done"]), sorted(receipt["candidate_ids"]))
        self.assertEqual(receipt["execution_order"], receipt["candidate_ids"])

    def test_m7_overlapping_native_roots_share_one_taskstore_pid(self) -> None:
        receipt = self._run_fixture(
            major=7,
            candidate_ids=[57, 61, 21],
            parents={"57": [21], "61": [21]},
            children={"21": [57, 61]},
        )
        self.assertEqual(receipt["launched_workers"], 1)
        self.assertEqual(len(receipt["task_pids"]), 1)
        self.assertEqual(receipt["blocked_native_attempts"], 0)
        self.assertEqual(receipt["done"], [21, 57, 61])
        self.assertEqual(receipt["execution_order"], [21, 57, 61])
        self.assertEqual(receipt["native_covered_ids"], [21, 57, 61])
        self.assertIn("--exclude=21", receipt["task_commands"][0])

    def test_m7_native_exclude_capability_probe_is_read_only_and_array_aware(self) -> None:
        class Store:
            def __init__(self) -> None:
                self.payloads: dict[str, dict[str, object]] = {}

            def get_runtime_sync(self, key: str) -> dict[str, object] | None:
                return self.payloads.get(key)

            def put_runtime_sync(self, key: str, payload: dict[str, object]) -> None:
                self.payloads[key] = payload

        store = Store()
        config = SimpleNamespace(php_bin="/usr/bin/php", mautic_run_as_user="")
        completed = SimpleNamespace(
            returncode=0,
            stdout='{"definition":{"options":{"exclude":{"name":"--exclude","is_multiple":true}}}}',
            stderr="",
        )
        with patch.object(daemon, "render_mautic_command", return_value=["php", "bin/console", "help"]), patch(
            "mcd_agent.daemon.subprocess.run", return_value=completed
        ) as run:
            self.assertTrue(
                daemon._segment_native_exclude_capability(
                    store,
                    root="/var/www/site",
                    config=config,
                    mautic_major=7,
                )
            )
        run.assert_called_once()
        payload = next(iter(store.payloads.values()))
        self.assertTrue(payload["read_only"])
        self.assertTrue(payload["supported"])


if __name__ == "__main__":
    unittest.main()
