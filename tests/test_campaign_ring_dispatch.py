from __future__ import annotations

import unittest
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcd_agent import daemon as daemon_mod
from mcd_agent.daemon import (
    CampaignTriggerProgressSnapshot,
    RunningTask,
    SQLSegmentRule,
    TaskStore,
    _CAMPAIGN_EMAIL_COUNTER_RECONCILE_AT,
    _CAMPAIGN_NATIVE_FALLBACK_TIMEOUT_SEC,
    _CAMPAIGN_REBUILD_FINISHED_AT,
    _CAMPAIGN_TRIGGER_STUCK_UNTIL,
    _PriorityTaskExecutor,
    _TASK_LOCK_BUSY_RC,
    _campaign_pressure_active,
    _campaign_dispatch_begin,
    _campaign_dispatch_end,
    _campaign_fallback_end,
    _campaign_fallback_try_begin,
    _campaign_native_fallback_last_run_after_completion,
    _campaign_whitelist_effective_setting,
    _campaign_rebuild_waits_for_trigger,
    _campaign_trigger_email_progress_sql,
    _campaign_trigger_event_log_due_exists_sql,
    _campaign_trigger_event_log_progress_sql,
    _campaign_trigger_progress_watchdog,
    _campaign_trigger_should_skip_launch,
    _campaign_trigger_waits_for_rebuild,
    _classify_import_monitor_row,
    _effective_segment_slot_limit,
    _fill_from_ring,
    _force_due_campaigns_to_priority,
    _import_pending_poll_due,
    _mark_campaign_rebuild_finished,
    _mark_campaign_trigger_finished,
    _merge_campaign_trigger_audit_ids,
    _monitor_running,
    _move_ring_entities_to_front,
    _plan_sql_segment_ring,
    _published_campaign_whitelist_ids,
    _published_segment_whitelist_ids,
    _priority_campaign_due_check_needed,
    _priority_interleaved_dispatch_installs,
    _recover_orphaned_imports_if_safe,
    _remove_ring_entities,
    _rotated_dispatch_installs,
    _run_sql_segment_ring,
    _scheduler_host_slots_available,
    _segment_sql_active_db_rebuild_query_count,
    _segment_shared_slots_available,
    _segment_task_limit_after_import,
    _segment_whitelist_effective_setting,
    _sync_segment_whitelist_file,
    _submit_import_if_segment_slot,
    _task_key,
    _task_execution_lock_key,
    _task_locked_args,
    _task_repeat_interval_sec,
)
from mcd_agent.ring_utils import advance_ring_after_launch


class CampaignRingDispatchTests(unittest.TestCase):
    def tearDown(self) -> None:
        _CAMPAIGN_TRIGGER_STUCK_UNTIL.clear()
        _CAMPAIGN_EMAIL_COUNTER_RECONCILE_AT.clear()
        _CAMPAIGN_REBUILD_FINISHED_AT.clear()
        daemon_mod._ENTITY_LAUNCH_GUARD.clear()
        with daemon_mod._SEGMENT_SQL_WORKERS_LOCK:
            daemon_mod._SEGMENT_SQL_WORKERS.clear()
        with daemon_mod._CAMPAIGN_FALLBACK_COORDINATOR_LOCK:
            daemon_mod._CAMPAIGN_FALLBACK_ACTIVE_ROOTS.clear()
            daemon_mod._CAMPAIGN_DISPATCHING_ROOTS.clear()

    def test_campaign_launch_can_remove_audit_only_id_from_current_ring(self) -> None:
        ring = deque([3, 4, 5])

        advance_ring_after_launch(ring, 3, remove_on_launch=True)

        self.assertEqual(list(ring), [4, 5])

    def test_host_scheduler_limit_counts_same_lane_tasks_across_instance_roots(self) -> None:
        running = {
            "a": SimpleNamespace(root="/var/www/a", task_type="segment"),
            "b": SimpleNamespace(root="/var/www/b", task_type="segment"),
        }
        cfg = SimpleNamespace(
            scheduler_host_max_parallel=2,
            segment_mode="id_weighted",
            segment_priority_parallel_idle=1,
            segment_regular_parallel_idle=1,
        )

        self.assertEqual(_scheduler_host_slots_available(cfg, running, "segment"), 0)

    def test_segment_and_campaign_host_slots_are_independent_in_both_directions(self) -> None:
        cfg = SimpleNamespace(
            scheduler_host_max_parallel=6,
            segment_mode="id_weighted",
            segment_priority_parallel_idle=3,
            segment_regular_parallel_idle=1,
            campaign_total_parallel=2,
        )
        prodajadelova = "/var/www/prodajadelova/public_html"
        segments_full = {
            f"segment-{idx}": SimpleNamespace(root=f"/var/www/segment-{idx}", task_type="segment")
            for idx in range(4)
        }
        campaigns_full = {
            f"campaign-{campaign_id}": SimpleNamespace(
                root=prodajadelova,
                task_type="campaign_rebuild",
                entity_id=campaign_id,
            )
            for campaign_id in (22, 17)
        }

        self.assertEqual(_scheduler_host_slots_available(cfg, segments_full, "segment"), 0)
        self.assertEqual(_scheduler_host_slots_available(cfg, segments_full, "campaign_rebuild"), 2)
        self.assertEqual(_scheduler_host_slots_available(cfg, campaigns_full, "campaign_rebuild"), 0)
        self.assertEqual(_scheduler_host_slots_available(cfg, campaigns_full, "segment"), 4)

    def test_dispatch_rotation_changes_first_instance_each_tick(self) -> None:
        installs = ["a", "b", "c"]

        self.assertEqual(_rotated_dispatch_installs(installs, 0), ["a", "b", "c"])
        self.assertEqual(_rotated_dispatch_installs(installs, 1), ["b", "c", "a"])
        self.assertEqual(_rotated_dispatch_installs(installs, 4), ["b", "c", "a"])

    def test_priority_dispatch_is_revisited_between_regular_chunks(self) -> None:
        priority = SimpleNamespace(root="/priority", instance_uid="priority@host")
        regular = [SimpleNamespace(root=f"/regular-{idx}", instance_uid=f"regular-{idx}@host") for idx in range(5)]
        cfg = SimpleNamespace(
            disable_whitelist=False,
            segment_whitelist=[],
            segment_whitelist_file=None,
            segment_whitelist_instance_settings={"priority@host": {"ids": [23]}},
            campaign_whitelist=[],
            campaign_whitelist_file=None,
            campaign_whitelist_instance_settings={},
        )

        planned = _priority_interleaved_dispatch_installs([*regular, priority], cfg, regular_chunk=2)

        self.assertEqual([inst.root for inst in planned], [
            "/priority", "/regular-0", "/regular-1",
            "/priority", "/regular-2", "/regular-3",
            "/priority", "/regular-4",
        ])

    def test_campaign_rebuild_and_trigger_share_execution_lock(self) -> None:
        root = "/var/www/electronic/public_html"

        rebuild = _task_execution_lock_key(root, "campaign_rebuild", 29)
        trigger = _task_execution_lock_key(root, "campaign_trigger", 29)
        other = _task_execution_lock_key(root, "campaign_trigger", 30)

        self.assertEqual(rebuild, trigger)
        self.assertNotEqual(trigger, other)

    def test_task_lock_wraps_command_with_stable_flock_path(self) -> None:
        args = ["php", "bin/console", "mautic:segments:update", "-i", "23"]
        with patch.object(Path, "is_file", return_value=True), patch.object(Path, "mkdir"):
            first = _task_locked_args("site|segment|23", args)
            second = _task_locked_args("site|segment|23", args)

        self.assertEqual(first, second)
        self.assertEqual(first[:4], ["/usr/bin/flock", "--nonblock", "--conflict-exit-code", "75"])
        self.assertEqual(first[-len(args):], args)

    def test_native_fallback_and_exact_campaigns_share_exclusive_root_gate(self) -> None:
        root = "/var/www/dexyco/public_html"
        args = ["php", "bin/console", "mautic:campaigns:trigger"]
        with patch.object(Path, "is_file", return_value=True), patch.object(Path, "mkdir"):
            exact = _task_locked_args(
                "dexyco|campaign|176",
                [*args, "-i", "176"],
                root=root,
                task_type="campaign_trigger",
            )
            fallback = _task_locked_args(
                "dexyco|campaign_native_fallback|0",
                args,
                root=root,
                task_type="campaign_native_fallback",
            )

        self.assertEqual(exact[1], "--shared")
        self.assertEqual(fallback[1], "--exclusive")
        self.assertEqual(exact[5], fallback[5])

    def test_native_fallback_coordinator_blocks_trigger_and_rebuild_but_not_segments(self) -> None:
        root = "/var/www/dexyco/public_html"
        self.assertTrue(
            _campaign_fallback_try_begin(
                root,
                campaign_worker_active=False,
                priority_campaign_active=False,
            )
        )
        self.assertFalse(_campaign_dispatch_begin(root, "campaign_trigger"))
        self.assertFalse(_campaign_dispatch_begin(root, "campaign_rebuild"))
        self.assertTrue(_campaign_dispatch_begin(root, "segment"))
        _campaign_dispatch_end(root, "segment")
        _campaign_fallback_end(root)

        self.assertTrue(_campaign_dispatch_begin(root, "campaign_trigger"))
        self.assertFalse(
            _campaign_fallback_try_begin(
                root,
                campaign_worker_active=False,
                priority_campaign_active=False,
            )
        )
        _campaign_dispatch_end(root, "campaign_trigger")

    def test_native_fallback_is_bounded_and_restarts_interval_after_completion(self) -> None:
        self.assertEqual(_CAMPAIGN_NATIVE_FALLBACK_TIMEOUT_SEC, 30 * 60)
        self.assertEqual(
            _campaign_native_fallback_last_run_after_completion(
                previous_run_ts=100.0,
                completed_at=500.0,
                rc=0,
            ),
            500.0,
        )
        self.assertEqual(
            _campaign_native_fallback_last_run_after_completion(
                previous_run_ts=100.0,
                completed_at=500.0,
                rc=None,
            ),
            500.0,
        )
        self.assertEqual(
            _campaign_native_fallback_last_run_after_completion(
                previous_run_ts=100.0,
                completed_at=500.0,
                rc=_TASK_LOCK_BUSY_RC,
            ),
            100.0,
        )

    def test_priority_executor_has_separate_bounded_lane(self) -> None:
        executor = _PriorityTaskExecutor()
        release = threading.Event()
        started = threading.Event()

        def _run(*_args: object, **_kwargs: object) -> SimpleNamespace:
            started.set()
            release.wait(timeout=2)
            return SimpleNamespace(pid=1234, wait=lambda timeout=None: 0)

        cfg = SimpleNamespace(command_timeout_sec=0)
        with patch.object(daemon_mod, "_spawn_command", side_effect=_run):
            self.assertTrue(
                executor.launch(
                    cfg,
                    root="/var/www/site",
                    task_type="segment",
                    entity_id=23,
                    args=["php", "bin/console"],
                    interval_sec=60,
                    max_parallel=1,
                )
            )
            self.assertTrue(started.wait(timeout=1))
            self.assertFalse(
                executor.launch(
                    cfg,
                    root="/var/www/site",
                    task_type="segment",
                    entity_id=24,
                    args=["php", "bin/console"],
                    interval_sec=60,
                    max_parallel=1,
                )
            )
            release.set()
            deadline = time.time() + 2
            while executor.is_active("/var/www/site", "segment", 23) and time.time() < deadline:
                time.sleep(0.01)

        self.assertFalse(executor.is_active("/var/www/site", "segment", 23))
        self.assertGreater(executor.last_finished("/var/www/site", "segment", 23), 0)

    def test_priority_executor_updates_normal_scheduler_repeat_guard(self) -> None:
        executor = _PriorityTaskExecutor()
        cfg = SimpleNamespace(command_timeout_sec=0, segment_full_scan_interval_sec=60)
        completed = threading.Event()

        with (
            patch.object(daemon_mod.time, "time", return_value=100.0),
            patch.object(
                daemon_mod,
                "_spawn_command",
                return_value=SimpleNamespace(pid=1235, wait=lambda timeout=None: 0),
            ),
        ):
            self.assertTrue(
                executor.launch(
                    cfg,
                    root="/var/www/site",
                    task_type="segment",
                    entity_id=23,
                    args=["php", "bin/console"],
                    interval_sec=60,
                    max_parallel=1,
                    on_success=completed.set,
                )
            )
            self.assertTrue(completed.wait(timeout=1))

        self.assertFalse(
            daemon_mod._launch_allowed(
                cfg,
                "/var/www/site",
                "segment",
                23,
                now_ts=130.0,
            )
        )
        self.assertTrue(
            daemon_mod._launch_allowed(
                cfg,
                "/var/www/site",
                "segment",
                23,
                now_ts=160.0,
            )
        )

    def test_priority_timeout_kills_the_spawned_process_group(self) -> None:
        executor = _PriorityTaskExecutor()
        completed = threading.Event()
        result: list[int | None] = []

        class TimedOutProcess:
            pid = 4321

            @staticmethod
            def wait(timeout: int | None = None) -> int:
                raise daemon_mod.subprocess.TimeoutExpired(["php"], timeout or 1)

        cfg = SimpleNamespace(command_timeout_sec=0, segment_kill_grace_sec=7)
        with (
            patch.object(daemon_mod, "_spawn_command", return_value=TimedOutProcess()),
            patch.object(daemon_mod, "_kill_pid") as kill_pid,
        ):
            self.assertTrue(
                executor.launch(
                    cfg,
                    root="/var/www/site",
                    task_type="campaign_native_fallback",
                    entity_id=0,
                    args=["php", "bin/console"],
                    interval_sec=60,
                    max_parallel=1,
                    timeout_sec=10,
                    on_complete=lambda rc: (result.append(rc), completed.set()),
                )
            )
            self.assertTrue(completed.wait(timeout=1))

        kill_pid.assert_called_once_with(4321, 7)
        self.assertEqual(result, [None])

    def test_priority_segment_timeout_is_forwarded_to_executor(self) -> None:
        executor = _PriorityTaskExecutor()
        observed: list[int | None] = []
        done = threading.Event()

        class Process:
            pid = 4322

            @staticmethod
            def wait(timeout: int | None = None) -> int:
                observed.append(timeout)
                return 0

        cfg = SimpleNamespace(command_timeout_sec=0, segment_kill_grace_sec=7)
        with patch.object(daemon_mod, "_spawn_command", return_value=Process()):
            self.assertTrue(
                executor.launch(
                    cfg,
                    root="/var/www/site",
                    task_type="segment",
                    entity_id=23,
                    args=["php", "bin/console"],
                    interval_sec=60,
                    max_parallel=1,
                    timeout_sec=3600,
                    on_complete=lambda _rc: done.set(),
                )
            )
            self.assertTrue(done.wait(timeout=1))

        self.assertEqual(observed, [3600])

    def test_kill_pid_signals_isolated_process_group(self) -> None:
        calls: list[tuple[int, int]] = []

        def _killpg(pgid: int, sig: int) -> None:
            calls.append((pgid, sig))

        with (
            patch.object(daemon_mod, "_is_pid_alive", return_value=True),
            patch.object(daemon_mod.os, "getpgid", return_value=7001),
            patch.object(daemon_mod.os, "getpgrp", return_value=8001),
            patch.object(daemon_mod.os, "killpg", side_effect=_killpg),
        ):
            daemon_mod._kill_pid(7001, 0)

        self.assertEqual(
            calls,
            [
                (7001, daemon_mod.signal.SIGTERM),
                (7001, 0),
                (7001, daemon_mod.signal.SIGKILL),
            ],
        )

    def test_priority_campaign_due_check_waits_for_its_first_rebuild(self) -> None:
        self.assertFalse(
            _priority_campaign_due_check_needed(
                now_ts=100.0,
                last_checked_ts=0.0,
                rebuild_started_ts=0.0,
                rebuild_finished_ts=0.0,
                checked_rebuild_ts=0.0,
                interval_sec=60,
            )
        )
        self.assertTrue(
            _priority_campaign_due_check_needed(
                now_ts=101.0,
                last_checked_ts=100.0,
                rebuild_started_ts=90.0,
                rebuild_finished_ts=101.0,
                checked_rebuild_ts=0.0,
                interval_sec=60,
            )
        )
        self.assertTrue(
            _priority_campaign_due_check_needed(
                now_ts=160.0,
                last_checked_ts=100.0,
                rebuild_started_ts=90.0,
                rebuild_finished_ts=101.0,
                checked_rebuild_ts=101.0,
                interval_sec=60,
            )
        )

    def test_monitor_treats_execution_lock_contention_as_clean_skip(self) -> None:
        root = "/var/www/site"
        key = _task_key(root, "campaign_trigger", 29)
        task = RunningTask(
            row_id=7,
            root=root,
            task_key=key,
            task_type="campaign_trigger",
            entity_id=29,
            command_str="php|bin/console|mautic:campaigns:trigger|-i|29",
            timeout_sec=0,
            attempts=1,
            started_at=time.time(),
            pid=1234,
        )
        proc = Mock()
        proc.poll.return_value = _TASK_LOCK_BUSY_RC
        store = Mock()
        running = {key: task}
        popens = {key: proc}

        with patch.object(daemon_mod, "_respawn_task") as respawn:
            _monitor_running(
                config=SimpleNamespace(),
                store=store,
                running=running,
                popens=popens,
            )

        store.finish.assert_called_once_with(7, state="done", rc=_TASK_LOCK_BUSY_RC, note="task_lock_busy")
        respawn.assert_not_called()
        self.assertNotIn(key, running)

    def test_campaign_trigger_retry_uses_targeted_backoff_when_task_retry_max_is_one(self) -> None:
        root = "/var/www/site"
        key = _task_key(root, "campaign_trigger", 29)
        task = RunningTask(
            row_id=7,
            root=root,
            task_key=key,
            task_type="campaign_trigger",
            entity_id=29,
            command_str="php|bin/console|mautic:campaigns:trigger|-i|29",
            timeout_sec=0,
            attempts=1,
            started_at=time.time(),
            pid=1234,
        )
        proc = Mock()
        proc.poll.return_value = 1
        store = Mock()
        running = {key: task}
        popens = {key: proc}

        with patch.object(daemon_mod, "_respawn_task") as respawn:
            _monitor_running(
                config=SimpleNamespace(task_retry_max=1, task_retry_delay_sec=0),
                store=store,
                running=running,
                popens=popens,
            )

        store.finish.assert_called_once_with(7, state="failed", rc=1, note="non_zero_exit")
        respawn.assert_called_once()
        self.assertIs(respawn.call_args.kwargs["store"], store)
        self.assertIs(respawn.call_args.kwargs["running"], running)
        self.assertIs(respawn.call_args.kwargs["popens"], popens)
        self.assertIs(respawn.call_args.kwargs["task"], task)
        self.assertEqual(respawn.call_args.kwargs["config"].task_retry_max, 1)
        self.assertEqual(respawn.call_args.kwargs["config"].task_retry_delay_sec, 0)

    def test_campaign_whitelist_is_scoped_to_matching_instance(self) -> None:
        cfg = SimpleNamespace(
            disable_whitelist=False,
            campaign_whitelist=[99],
            campaign_whitelist_file=None,
            campaign_whitelist_instance_settings={"electronic@host": {"campaign_whitelist": [29]}},
        )
        electronic = SimpleNamespace(
            root="/var/www/electronic/public_html",
            instance_uid="electronic@host",
            primary_domain="electronic.sales-snap.com",
            name="electronic.sales-snap.com",
            domains=[],
        )
        other = SimpleNamespace(
            root="/var/www/other/public_html",
            instance_uid="other@host",
            primary_domain="other.sales-snap.com",
            name="other.sales-snap.com",
            domains=[],
        )

        self.assertEqual(_campaign_whitelist_effective_setting(cfg, electronic), {29})
        self.assertEqual(_campaign_whitelist_effective_setting(cfg, other), {99})

    def test_priority_campaign_skip_keeps_candidate_for_fast_recheck(self) -> None:
        ring = deque([29])
        cfg = SimpleNamespace(campaign_trigger_min_repeat_sec=0, campaign_trigger_audit_interval_sec=0)

        launched = _fill_from_ring(
            ring=ring,
            ring_limit=1,
            total_limit=1,
            root="/var/www/electronic/public_html",
            task_type="campaign_trigger",
            running={},
            ring_entities={29},
            config=cfg,
            store=Mock(),
            popens={},
            build_args=Mock(),
            should_skip=lambda _campaign_id: True,
            remove_on_skip=False,
            remove_on_launch=False,
        )

        self.assertEqual(launched, 0)
        self.assertEqual(list(ring), [29])

    def test_priority_campaign_retention_does_not_keep_weight_only_candidate(self) -> None:
        ring = deque([29, 30])
        cfg = SimpleNamespace(campaign_trigger_min_repeat_sec=0, campaign_trigger_audit_interval_sec=0)

        _fill_from_ring(
            ring=ring,
            ring_limit=1,
            total_limit=1,
            root="/var/www/electronic/public_html",
            task_type="campaign_trigger",
            running={},
            ring_entities={29, 30},
            config=cfg,
            store=Mock(),
            popens={},
            build_args=Mock(),
            should_skip=lambda _campaign_id: True,
            remove_on_skip=lambda campaign_id: campaign_id != 29,
            remove_on_launch=lambda campaign_id: campaign_id != 29,
        )

        self.assertEqual(list(ring), [29])

    def test_fill_from_ring_skips_stale_campaign_without_launching(self) -> None:
        ring = deque([136])
        cfg = SimpleNamespace(campaign_trigger_min_repeat_sec=0, campaign_trigger_audit_interval_sec=0)
        store = Mock()
        running: dict[str, RunningTask] = {}

        launched = _fill_from_ring(
            ring=ring,
            ring_limit=1,
            total_limit=1,
            root="/var/www/site",
            task_type="campaign_trigger",
            running=running,
            ring_entities={136},
            config=cfg,
            store=store,
            popens={},
            build_args=Mock(return_value=["php", "bin/console", "mautic:campaigns:trigger", "-i", "136"]),
            should_skip=lambda eid: True,
            remove_on_launch=True,
        )

        self.assertEqual(launched, 0)
        self.assertEqual(list(ring), [])
        store.add_running.assert_not_called()

    def test_fill_from_ring_bounds_stale_campaign_preflight_checks(self) -> None:
        # Audit candidates can be complete for months. Their due checks must
        # not consume the full shared-host scheduler pass before a fresh
        # campaign on a later root is considered.
        ring = deque(range(1, daemon_mod._CAMPAIGN_TRIGGER_STALE_SCAN_CAP + 3))
        cfg = SimpleNamespace(campaign_trigger_min_repeat_sec=0, campaign_trigger_audit_interval_sec=0)
        store = Mock()
        checked: list[int] = []

        launched = _fill_from_ring(
            ring=ring,
            ring_limit=1,
            total_limit=1,
            root="/var/www/site",
            task_type="campaign_trigger",
            running={},
            ring_entities=set(ring),
            config=cfg,
            store=store,
            popens={},
            build_args=Mock(),
            should_skip=lambda eid: checked.append(eid) or True,
            remove_on_launch=True,
        )

        self.assertEqual(launched, 0)
        self.assertEqual(checked, list(range(1, daemon_mod._CAMPAIGN_TRIGGER_STALE_SCAN_CAP + 1)))
        self.assertEqual(list(ring), [daemon_mod._CAMPAIGN_TRIGGER_STALE_SCAN_CAP + 1, daemon_mod._CAMPAIGN_TRIGGER_STALE_SCAN_CAP + 2])
        store.add_running.assert_not_called()

    def test_campaign_trigger_progress_watchdog_kills_done_stale_process(self) -> None:
        root = "/var/www/site"
        key = _task_key(root, "campaign_trigger", 136)
        task = RunningTask(
            row_id=42,
            root=root,
            task_key=key,
            task_type="campaign_trigger",
            entity_id=136,
            command_str="php bin/console mautic:campaigns:trigger -i 136",
            timeout_sec=0,
            attempts=1,
            started_at=100.0,
            pid=999999,
        )
        running = {key: task}
        store = Mock()
        snapshot = CampaignTriggerProgressSnapshot(
            due_event_logs=0,
            due_root_actions=0,
            due_no_action_branches=0,
            pending_event_logs=0,
            triggered_event_logs=4439,
            max_triggered_at="2026-06-12 08:25:59",
        )
        state = {
            key: {
                "last_check": 0.0,
                "progress_key": snapshot.progress_key(),
                "stable_checks": 1,
            }
        }
        cfg = SimpleNamespace(
            campaign_trigger_progress_watchdog_enabled=True,
            campaign_trigger_progress_watchdog_grace_sec=1,
            campaign_trigger_progress_watchdog_interval_sec=10,
            campaign_trigger_progress_watchdog_stable_checks=2,
            segment_kill_grace_sec=1,
        )

        with (
            patch.object(daemon_mod, "_campaign_trigger_progress_snapshot", return_value=snapshot),
            patch.object(daemon_mod, "_kill_pid") as kill_pid,
        ):
            stopped = _campaign_trigger_progress_watchdog(
                config=cfg,
                store=store,
                running=running,
                popens={},
                task=task,
                key=key,
                db_configs_by_root={root: object()},
                mautic_timezones_by_root={root: "Europe/Belgrade"},
                state_by_key=state,
                now_ts=500.0,
            )

        self.assertTrue(stopped)
        kill_pid.assert_called_once_with(999999, 1)
        store.finish.assert_called_once_with(42, state="done", rc=0, note="killed_no_due_no_progress")
        self.assertNotIn(key, running)
        self.assertNotIn(key, state)

    def test_campaign_trigger_progress_watchdog_kills_stuck_due_process_and_cools_down(self) -> None:
        root = "/var/www/site"
        key = _task_key(root, "campaign_trigger", 136)
        task = RunningTask(
            row_id=43,
            root=root,
            task_key=key,
            task_type="campaign_trigger",
            entity_id=136,
            command_str="php bin/console mautic:campaigns:trigger -i 136",
            timeout_sec=0,
            attempts=1,
            started_at=100.0,
            pid=999998,
        )
        running = {key: task}
        store = Mock()
        snapshot = CampaignTriggerProgressSnapshot(
            due_event_logs=1,
            due_root_actions=0,
            due_no_action_branches=0,
            pending_event_logs=1,
            triggered_event_logs=2219,
            max_triggered_at="2026-06-12 06:04:16",
        )
        state = {
            key: {
                "last_check": 0.0,
                "progress_key": snapshot.progress_key(),
                "stable_checks": 1,
            }
        }
        cfg = SimpleNamespace(
            campaign_trigger_progress_watchdog_enabled=True,
            campaign_trigger_progress_watchdog_grace_sec=1,
            campaign_trigger_progress_watchdog_interval_sec=10,
            campaign_trigger_progress_watchdog_stable_checks=2,
            segment_kill_grace_sec=1,
        )

        with (
            patch.object(daemon_mod, "_campaign_trigger_progress_snapshot", return_value=snapshot),
            patch.object(
                daemon_mod,
                "_campaign_trigger_latest_failed_reason",
                return_value="2026-06-14 15:30:08 Connection timed out.",
            ),
            patch.object(daemon_mod, "_kill_pid") as kill_pid,
        ):
            stopped = _campaign_trigger_progress_watchdog(
                config=cfg,
                store=store,
                running=running,
                popens={},
                task=task,
                key=key,
                db_configs_by_root={root: object()},
                mautic_timezones_by_root={root: "Europe/Belgrade"},
                state_by_key=state,
                now_ts=500.0,
            )

        self.assertTrue(stopped)
        kill_pid.assert_called_once_with(999998, 1)
        store.finish.assert_called_once_with(43, state="done", rc=0, note="killed_stuck_due_no_progress")
        self.assertNotIn(key, running)
        self.assertNotIn(key, state)
        until, reason = _CAMPAIGN_TRIGGER_STUCK_UNTIL[(root, 136)]
        self.assertGreater(until, 500.0)
        self.assertIn("Connection timed out", reason)

    def test_campaign_trigger_progress_watchdog_keeps_active_email_send_running(self) -> None:
        root = "/var/www/hotelsunce/public_html"
        key = _task_key(root, "campaign_trigger", 57)
        task = RunningTask(
            row_id=44,
            root=root,
            task_key=key,
            task_type="campaign_trigger",
            entity_id=57,
            command_str="php bin/console mautic:campaigns:trigger -i 57",
            timeout_sec=0,
            attempts=1,
            started_at=100.0,
            pid=999997,
        )
        previous = CampaignTriggerProgressSnapshot(
            due_event_logs=1,
            due_root_actions=0,
            due_no_action_branches=0,
            pending_event_logs=1,
            triggered_event_logs=3975,
            max_triggered_at="2026-08-08 08:00:00",
            sent_email_stats=3432,
            max_email_sent_at="2026-08-08 08:30:00",
        )
        current = CampaignTriggerProgressSnapshot(
            due_event_logs=1,
            due_root_actions=0,
            due_no_action_branches=0,
            pending_event_logs=1,
            triggered_event_logs=3975,
            max_triggered_at="2026-08-08 08:00:00",
            sent_email_stats=5098,
            max_email_sent_at="2026-08-08 08:31:00",
        )
        state = {
            key: {
                "last_check": 0.0,
                "progress_key": previous.progress_key(),
                "stable_checks": 1,
            }
        }
        cfg = SimpleNamespace(
            campaign_trigger_progress_watchdog_enabled=True,
            campaign_trigger_progress_watchdog_grace_sec=1,
            campaign_trigger_progress_watchdog_interval_sec=10,
            campaign_trigger_progress_watchdog_stable_checks=2,
            segment_kill_grace_sec=1,
        )

        with (
            patch.object(daemon_mod, "_campaign_trigger_progress_snapshot", return_value=current),
            patch.object(daemon_mod, "_kill_pid") as kill_pid,
        ):
            stopped = _campaign_trigger_progress_watchdog(
                config=cfg,
                store=Mock(),
                running={key: task},
                popens={},
                task=task,
                key=key,
                db_configs_by_root={root: object()},
                mautic_timezones_by_root={root: "Europe/Belgrade"},
                state_by_key=state,
                now_ts=500.0,
            )

        self.assertFalse(stopped)
        kill_pid.assert_not_called()
        self.assertEqual(state[key]["stable_checks"], 0)
        self.assertEqual(state[key]["progress_key"], current.progress_key())

    def test_campaign_trigger_guard_skips_active_stuck_cooldown(self) -> None:
        root = "/var/www/site"
        _CAMPAIGN_TRIGGER_STUCK_UNTIL[(root, 136)] = (600.0, "smtp timeout")

        with (
            patch.object(daemon_mod.time, "time", return_value=500.0),
            patch.object(daemon_mod, "_campaign_trigger_progress_snapshot") as snapshot,
        ):
            skipped = _campaign_trigger_should_skip_launch(
                db=Mock(),
                config=SimpleNamespace(campaign_trigger_due_guard_enabled=True),
                root=root,
                campaign_id=136,
                sql_ctx={},
            )

        self.assertTrue(skipped)
        snapshot.assert_not_called()
        self.assertIn((root, 136), _CAMPAIGN_TRIGGER_STUCK_UNTIL)

    def test_campaign_trigger_guard_reconciles_stale_complete_email_counter(self) -> None:
        root = "/var/www/site"
        snapshot = CampaignTriggerProgressSnapshot(
            due_event_logs=0,
            due_root_actions=0,
            due_no_action_branches=0,
            pending_event_logs=0,
            triggered_event_logs=169709,
            max_triggered_at="2026-06-30 08:27:02",
        )
        db = Mock()

        with (
            patch.object(daemon_mod.time, "time", return_value=1782817200.0),
            patch.object(daemon_mod, "_campaign_trigger_progress_snapshot", return_value=snapshot),
            patch.object(
                daemon_mod,
                "repair_campaign_email_counters",
                return_value={"checked": 1, "mismatches": 1, "repaired": 1, "skipped": 0},
            ) as repair,
        ):
            skipped = _campaign_trigger_should_skip_launch(
                db=db,
                config=SimpleNamespace(campaign_trigger_due_guard_enabled=True),
                root=root,
                campaign_id=557,
                sql_ctx={},
            )

        self.assertTrue(skipped)
        repair.assert_called_once_with(db, 557)

    def test_campaign_trigger_guard_counts_prescheduled_rows_as_due(self) -> None:
        due_sql = _campaign_trigger_event_log_due_exists_sql(21)
        progress_sql = _campaign_trigger_event_log_progress_sql(21)
        email_progress_sql = _campaign_trigger_email_progress_sql(21)

        self.assertIn("el.is_scheduled = 1", due_sql)
        self.assertNotIn("el.date_triggered IS NULL", due_sql)
        self.assertNotIn("el.date_triggered < el.trigger_date", due_sql)
        self.assertIn("el.trigger_date <= '{now_event_log}'", due_sql)
        self.assertIn("c.publish_down IS NULL OR c.publish_down >= '{now_utc}'", due_sql)

        self.assertIn("el.is_scheduled = 1", progress_sql)
        self.assertIn("SUM(CASE WHEN el.is_scheduled = 1 THEN 1 ELSE 0 END)", progress_sql)
        self.assertIn("pending_event_logs", progress_sql)
        self.assertIn("es.source = 'campaign.event'", email_progress_sql)
        self.assertIn("ce.id = es.source_id", email_progress_sql)
        self.assertIn("ce.campaign_id = 21", email_progress_sql)
        self.assertIn("MAX(es.date_sent)", email_progress_sql)

    def test_non_campaign_rings_keep_round_robin_rotation(self) -> None:
        ring = deque([3, 4, 5])

        advance_ring_after_launch(ring, 3)

        self.assertEqual(list(ring), [4, 5, 3])

    def test_remove_after_prior_rotation_still_deletes_launched_id(self) -> None:
        ring = deque([3, 4, 5])
        ring.rotate(-1)

        advance_ring_after_launch(ring, 3, remove_on_launch=True)

        self.assertEqual(list(ring), [4, 5])

    def test_campaign_trigger_repeat_guard_uses_one_minute_audit_floor(self) -> None:
        cfg = SimpleNamespace(
            campaign_trigger_min_repeat_sec=10,
            campaign_trigger_audit_interval_sec=60,
        )

        self.assertEqual(_task_repeat_interval_sec(cfg, "campaign_trigger"), 60)

    def test_campaign_trigger_audit_ids_persist_between_due_sql_cycles(self) -> None:
        audit_ids = [684, 683, 681, 678, 668, 667, 657, 656]

        first_plan = _merge_campaign_trigger_audit_ids([684, 683], audit_ids)
        next_plan = _merge_campaign_trigger_audit_ids([684, 683], audit_ids)

        self.assertIn(656, first_plan)
        self.assertIn(656, next_plan)
        self.assertEqual(first_plan.index(656), next_plan.index(656))

    def test_due_campaign_stays_ahead_of_readded_stale_audit_ids(self) -> None:
        due_id = 1065
        stale_ids = list(range(900, 1020))
        priority, regular = _force_due_campaigns_to_priority(
            stale_ids,
            [due_id],
            [due_id],
        )
        self.assertEqual(priority[0], due_id)
        self.assertNotIn(due_id, regular)

        old_priority = deque([due_id])
        refreshed_priority, _ = daemon_mod._reconcile_campaign_rings(
            old_priority,
            deque(),
            priority,
            regular,
        )
        self.assertNotEqual(refreshed_priority[0], due_id)

        _move_ring_entities_to_front(refreshed_priority, [due_id])

        self.assertEqual(refreshed_priority[0], due_id)
        self.assertEqual(len(refreshed_priority), len(stale_ids) + 1)

    def test_campaign_trigger_waits_for_rebuild_after_plan(self) -> None:
        _CAMPAIGN_REBUILD_FINISHED_AT.clear()
        root = "/var/www/site"

        self.assertTrue(
            _campaign_trigger_waits_for_rebuild(
                root=root,
                campaign_id=656,
                planned_after_ts=100.0,
                running={},
            )
        )

        _mark_campaign_rebuild_finished(root, 656, now_ts=101.0)

        self.assertFalse(
            _campaign_trigger_waits_for_rebuild(
                root=root,
                campaign_id=656,
                planned_after_ts=100.0,
                running={},
            )
        )

        _mark_campaign_trigger_finished(root, 656)

        self.assertTrue(
            _campaign_trigger_waits_for_rebuild(
                root=root,
                campaign_id=656,
                planned_after_ts=100.0,
                running={},
            )
        )

    def test_campaign_rebuild_waits_for_running_trigger_same_campaign(self) -> None:
        root = "/var/www/site"
        running = {
            _task_key(root, "campaign_trigger", 163): RunningTask(
                row_id=1,
                root=root,
                task_key=_task_key(root, "campaign_trigger", 163),
                task_type="campaign_trigger",
                entity_id=163,
                command_str="campaign trigger 163",
                timeout_sec=3600,
                attempts=1,
                started_at=1.0,
                pid=1001,
            ),
            _task_key(root, "campaign_trigger", 164): RunningTask(
                row_id=2,
                root=root,
                task_key=_task_key(root, "campaign_trigger", 164),
                task_type="campaign_trigger",
                entity_id=164,
                command_str="campaign trigger 164",
                timeout_sec=3600,
                attempts=1,
                started_at=1.0,
                pid=1002,
            ),
        }

        self.assertTrue(
            _campaign_rebuild_waits_for_trigger(
                root=root,
                campaign_id=163,
                running=running,
            )
        )
        self.assertFalse(
            _campaign_rebuild_waits_for_trigger(
                root=root,
                campaign_id=165,
                running=running,
            )
        )

    def test_campaign_rebuild_ring_skips_id_with_running_trigger(self) -> None:
        root = "/var/www/site"
        ring = deque([163, 165])
        cfg = SimpleNamespace(campaign_rebuild_min_repeat_sec=0, command_timeout_sec=3600)
        store = Mock()
        store.has_running_task_key.return_value = False
        store.last_task_started_at.return_value = 0
        store.add_running.return_value = 123
        running = {
            _task_key(root, "campaign_trigger", 163): RunningTask(
                row_id=1,
                root=root,
                task_key=_task_key(root, "campaign_trigger", 163),
                task_type="campaign_trigger",
                entity_id=163,
                command_str="campaign trigger 163",
                timeout_sec=3600,
                attempts=1,
                started_at=1.0,
                pid=1001,
            )
        }

        fake_proc = Mock()
        fake_proc.pid = 2002
        with patch.object(daemon_mod, "_spawn_command", return_value=fake_proc):
            launched = _fill_from_ring(
                ring=ring,
                ring_limit=1,
                total_limit=1,
                root=root,
                task_type="campaign_rebuild",
                running=running,
                ring_entities={163, 165},
                config=cfg,
                store=store,
                popens={},
                build_args=lambda cid: ["php", "bin/console", "mautic:campaigns:rebuild", "-i", str(cid)],
                dynamic_blocked=lambda cid: _campaign_rebuild_waits_for_trigger(
                    root=root,
                    campaign_id=cid,
                    running=running,
                ),
            )

        self.assertEqual(launched, 1)
        store.add_running.assert_called_once()
        added_task = store.add_running.call_args.args[0]
        self.assertEqual(added_task.task_type, "campaign_rebuild")
        self.assertEqual(added_task.entity_id, 165)
        self.assertIn(163, ring)

    def test_task_store_persists_last_launch_for_restart_guard(self) -> None:
        with tempfile.NamedTemporaryFile() as tmp:
            store = TaskStore(tmp.name)
            key = _task_key("/var/www/site", "campaign_trigger", 104)
            task = RunningTask(
                row_id=0,
                root="/var/www/site",
                task_key=key,
                task_type="campaign_trigger",
                entity_id=104,
                command_str="php bin/console mautic:campaigns:trigger -i 104",
                timeout_sec=3600,
                attempts=1,
                started_at=12345.0,
                pid=999999,
            )
            store.add_running(task)

            self.assertEqual(store.last_task_started_at(key), 12345.0)

    def test_import_consumes_one_shared_segment_slot(self) -> None:
        root = "/var/www/site"
        running = {
            _task_key(root, "segment", 23): RunningTask(
                row_id=1,
                root=root,
                task_key=_task_key(root, "segment", 23),
                task_type="segment",
                entity_id=23,
                command_str="segment 23",
                timeout_sec=3600,
                attempts=1,
                started_at=1.0,
                pid=1001,
            ),
            _task_key(root, "segment", 44): RunningTask(
                row_id=2,
                root=root,
                task_key=_task_key(root, "segment", 44),
                task_type="segment",
                entity_id=44,
                command_str="segment 44",
                timeout_sec=3600,
                attempts=1,
                started_at=1.0,
                pid=1002,
            ),
            _task_key(root, "import", None): RunningTask(
                row_id=3,
                root=root,
                task_key=_task_key(root, "import", None),
                task_type="import",
                entity_id=None,
                command_str="import",
                timeout_sec=3600,
                attempts=1,
                started_at=1.0,
                pid=1003,
            ),
        }

        self.assertEqual(_segment_shared_slots_available(running, root, 4), 1)
        self.assertEqual(_segment_task_limit_after_import(running, root, 4), 3)

    def test_pending_import_waits_when_shared_segment_slots_are_full(self) -> None:
        root = "/var/www/site"
        running = {
            _task_key(root, "segment", idx): RunningTask(
                row_id=idx,
                root=root,
                task_key=_task_key(root, "segment", idx),
                task_type="segment",
                entity_id=idx,
                command_str=f"segment {idx}",
                timeout_sec=3600,
                attempts=1,
                started_at=1.0,
                pid=1000 + idx,
            )
            for idx in (23, 44, 55, 123)
        }
        cfg = SimpleNamespace(
            enable_import_polling=True,
            php_bin="php",
            mautic_run_as_user="www-data",
            cmd_import_template="bin/console mautic:import --limit={import_limit}",
            import_limit=100,
            command_timeout_sec=3600,
        )

        with (
            patch.object(daemon_mod, "render_mautic_command", return_value=["php", "bin/console", "mautic:import"]),
            patch.object(daemon_mod, "_submit_if_slot", return_value=True) as submit,
        ):
            launched = _submit_import_if_segment_slot(
                config=cfg,
                store=SimpleNamespace(),
                running=running,
                popens={},
                root=root,
                cluster_import_allowed=True,
                import_pending_count=1,
                segment_slot_limit=4,
                now_ts=100.0,
            )

        self.assertFalse(launched)
        submit.assert_not_called()

    def test_pending_import_claims_next_free_shared_segment_slot(self) -> None:
        root = "/var/www/site"
        running = {
            _task_key(root, "segment", idx): RunningTask(
                row_id=idx,
                root=root,
                task_key=_task_key(root, "segment", idx),
                task_type="segment",
                entity_id=idx,
                command_str=f"segment {idx}",
                timeout_sec=3600,
                attempts=1,
                started_at=1.0,
                pid=1000 + idx,
            )
            for idx in (23, 44, 55)
        }
        cfg = SimpleNamespace(
            enable_import_polling=True,
            php_bin="php",
            mautic_run_as_user="www-data",
            cmd_import_template="bin/console mautic:import --limit={import_limit}",
            import_limit=100,
            command_timeout_sec=3600,
        )

        with (
            patch.object(daemon_mod, "render_mautic_command", return_value=["php", "bin/console", "mautic:import"]),
            patch.object(daemon_mod, "_submit_if_slot", return_value=True) as submit,
        ):
            launched = _submit_import_if_segment_slot(
                config=cfg,
                store=SimpleNamespace(),
                running=running,
                popens={},
                root=root,
                cluster_import_allowed=True,
                import_pending_count=2,
                segment_slot_limit=4,
                now_ts=100.0,
            )

        self.assertTrue(launched)
        self.assertEqual(submit.call_args.kwargs["task_type"], "import")
        self.assertEqual(submit.call_args.kwargs["entity_id"], None)
        self.assertEqual(submit.call_args.kwargs["max_parallel_for_type"], 1)

    def test_import_pending_poll_fast_follows_recent_import_activity(self) -> None:
        root = "/var/www/site"
        cfg = SimpleNamespace(import_poll_interval_sec=15)
        running: dict[str, RunningTask] = {}

        self.assertTrue(
            _import_pending_poll_due(
                config=cfg,
                root=root,
                now_ts=100.0,
                last_poll_ts={root: 95.0},
                last_activity_ts={root: 99.0},
                running=running,
                pending_cache={root: 0},
            )
        )

    def test_import_pending_poll_does_not_fast_poll_while_import_running(self) -> None:
        root = "/var/www/site"
        cfg = SimpleNamespace(import_poll_interval_sec=15)
        running = {
            _task_key(root, "import", None): RunningTask(
                row_id=1,
                root=root,
                task_key=_task_key(root, "import", None),
                task_type="import",
                entity_id=None,
                command_str="import",
                timeout_sec=3600,
                attempts=1,
                started_at=90.0,
                pid=1001,
            )
        }

        self.assertFalse(
            _import_pending_poll_due(
                config=cfg,
                root=root,
                now_ts=100.0,
                last_poll_ts={root: 95.0},
                last_activity_ts={root: 99.0},
                running=running,
                pending_cache={root: 0},
            )
        )

    def test_import_monitor_uses_mautic_4_to_7_status_constants(self) -> None:
        self.assertEqual(_classify_import_monitor_row({"status": 1}), ("queued", "", "queued"))
        self.assertEqual(_classify_import_monitor_row({"status": 2}), ("running", "", "processing"))
        self.assertEqual(_classify_import_monitor_row({"status": 3}), ("done", "", "success"))
        self.assertEqual(_classify_import_monitor_row({"status": 4}), ("done", "", "error"))
        self.assertEqual(_classify_import_monitor_row({"status": 5}), ("queued", "stopped", "stopped"))
        self.assertEqual(_classify_import_monitor_row({"status": 6}), ("running", "", "processing"))
        self.assertEqual(_classify_import_monitor_row({"status": 7}), ("queued", "delayed", "delayed"))

    def test_orphaned_import_recovery_skips_when_cli_worker_is_alive(self) -> None:
        db = SimpleNamespace(recover_orphaned_imports=Mock(return_value=1))

        with patch("mcd_agent.daemon._root_has_live_import_process", return_value=True):
            recovered = _recover_orphaned_imports_if_safe(db, "/var/www/mautic", {}, grace_sec=60)

        self.assertEqual(recovered, 0)
        db.recover_orphaned_imports.assert_not_called()

    def test_orphaned_import_recovery_requeues_when_cli_worker_is_absent(self) -> None:
        db = SimpleNamespace(recover_orphaned_imports=Mock(return_value=2))

        with patch("mcd_agent.daemon._root_has_live_import_process", return_value=False):
            recovered = _recover_orphaned_imports_if_safe(db, "/var/www/mautic", {}, grace_sec=60)

        self.assertEqual(recovered, 2)
        db.recover_orphaned_imports.assert_called_once_with("/var/www/mautic/var/import", grace_sec=60)

    def test_effective_segment_slot_limit_matches_throttled_profiles(self) -> None:
        cfg = SimpleNamespace(
            segment_mode="id_weighted",
            segment_throttle_whitelist_only=False,
            segment_throttle_whitelist_parallel=1,
            segment_priority_parallel_idle=3,
            segment_regular_parallel_idle=1,
            segment_priority_parallel_throttled=2,
            segment_regular_parallel_throttled=0,
        )

        self.assertEqual(_effective_segment_slot_limit(cfg, False), 4)
        self.assertEqual(_effective_segment_slot_limit(cfg, True), 2)

        cfg.segment_throttle_whitelist_only = True
        self.assertEqual(_effective_segment_slot_limit(cfg, True), 1)

    def test_fill_from_ring_launches_only_one_entity_per_scheduler_pass(self) -> None:
        root = "/var/www/site"
        ring = deque([11, 22, 33])
        cfg = SimpleNamespace(command_timeout_sec=3600, segment_full_scan_interval_sec=0)

        with patch.object(daemon_mod, "_submit_if_slot", return_value=True) as submit:
            launched = _fill_from_ring(
                ring=ring,
                ring_limit=3,
                total_limit=3,
                root=root,
                task_type="segment",
                running={},
                ring_entities={11, 22, 33},
                config=cfg,
                store=SimpleNamespace(),
                popens={},
                build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
            )

        self.assertEqual(launched, 1)
        submit.assert_called_once()
        self.assertEqual(submit.call_args.kwargs["entity_id"], 11)
        self.assertEqual(list(ring), [22, 33, 11])

    def test_fill_from_ring_can_fill_midi_priority_lane_in_one_pass(self) -> None:
        root = "/var/www/site"
        ring = deque([11, 22, 33, 44])
        cfg = SimpleNamespace(command_timeout_sec=3600, segment_full_scan_interval_sec=0)

        with patch.object(daemon_mod, "_submit_if_slot", return_value=True) as submit:
            launched = _fill_from_ring(
                ring=ring,
                ring_limit=3,
                total_limit=4,
                root=root,
                task_type="segment",
                running={},
                ring_entities={11, 22, 33, 44},
                config=cfg,
                store=SimpleNamespace(),
                popens={},
                build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
                max_launches=3,
            )

        self.assertEqual(launched, 3)
        self.assertEqual([call.kwargs["entity_id"] for call in submit.call_args_list], [11, 22, 33])
        self.assertEqual(list(ring), [44, 11, 22, 33])

    def test_fill_from_ring_skips_blocked_dependency_chain_and_launches_independent_segment(self) -> None:
        root = "/var/www/site"
        ring = deque([11, 22, 33])
        cfg = SimpleNamespace(command_timeout_sec=3600, segment_full_scan_interval_sec=0)

        with patch.object(daemon_mod, "_submit_if_slot", return_value=True) as submit:
            launched = _fill_from_ring(
                ring=ring,
                ring_limit=3,
                total_limit=3,
                root=root,
                task_type="segment",
                running={},
                ring_entities={11, 22, 33},
                config=cfg,
                store=SimpleNamespace(),
                popens={},
                build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
                dynamic_blocked=lambda sid: sid == 11,
            )

        self.assertEqual(launched, 1)
        submit.assert_called_once()
        self.assertEqual(submit.call_args.kwargs["entity_id"], 22)
        self.assertEqual(list(ring), [33, 11, 22])

    def test_sql_segment_ring_respects_dependency_chain_worker_lock(self) -> None:
        root = "/var/www/site"
        db = SimpleNamespace(rebuild_segment_membership=Mock(return_value={}))
        cfg = SimpleNamespace(segment_sql_ring_enabled=True)
        store = SimpleNamespace()
        ring = deque([22])

        launched = _run_sql_segment_ring(
            config=cfg,
            store=store,
            db=db,
            root=root,
            ring=ring,
            rules={22: SQLSegmentRule(segment_id=22, select_sql="SELECT 1", depends_on=())},
            active_set={22},
            done_set=set(),
            running={},
            sql_ctx={},
            now_ts=100.0,
            now_local=datetime.now(timezone.utc),
            dynamic_blocked=lambda sid: sid == 22,
        )

        self.assertEqual(launched, 0)
        db.rebuild_segment_membership.assert_not_called()
        self.assertEqual(list(ring), [22])

    def test_sql_segment_ring_refreshes_mautic_count_cache_after_rebuild(self) -> None:
        root = "/var/www/site"
        db = SimpleNamespace(
            rebuild_segment_membership=Mock(
                return_value={"selected_count": 17, "inserted_count": 15, "deleted_count": 3}
            )
        )
        cfg = SimpleNamespace(
            segment_sql_ring_enabled=True,
            segment_sql_ring_max_per_tick=1,
            segment_sql_statement_timeout_sec=1800,
            segment_sql_page_hits_quiet_only=False,
            segment_sql_min_repeat_sec=0,
            segment_sql_lock_heartbeat_sec=15,
            php_bin="/usr/bin/php",
            mautic_run_as_user="www-data",
        )
        store = SimpleNamespace()
        ring = deque([22])
        fake_stop = SimpleNamespace(set=Mock())
        fake_thread = SimpleNamespace(join=Mock())

        with (
            patch.object(daemon_mod, "_state_node_id", return_value="node-a"),
            patch.object(daemon_mod, "_segment_sql_try_acquire", return_value=(True, {})),
            patch.object(daemon_mod, "_segment_sql_start_heartbeat", return_value=(fake_stop, fake_thread)),
            patch.object(daemon_mod, "_segment_sql_finish") as finish,
            patch.object(daemon_mod, "_refresh_mautic_segment_count_cache", return_value=True) as refresh_cache,
        ):
            launched = _run_sql_segment_ring(
                config=cfg,
                store=store,
                db=db,
                root=root,
                ring=ring,
                rules={22: SQLSegmentRule(segment_id=22, select_sql="SELECT 1 AS lead_id", depends_on=())},
                active_set={22},
                done_set=set(),
                running={},
                sql_ctx={},
                now_ts=100.0,
                now_local=datetime.now(timezone.utc),
            )

        self.assertEqual(launched, 1)
        refresh_cache.assert_called_once_with(
            root=root,
            segment_id=22,
            count=15,
            php_bin="/usr/bin/php",
            run_as_user="www-data",
        )
        finish.assert_called_once()
        fake_stop.set.assert_called_once()
        fake_thread.join.assert_called_once_with(timeout=2.0)

    def test_sql_segment_long_ring_runs_async_worker_and_claims_slot(self) -> None:
        root = "/var/www/site"
        started = threading.Event()
        release = threading.Event()

        def rebuild_segment_membership(**_kwargs):
            started.set()
            release.wait(timeout=2.0)
            return {"selected_count": 101, "inserted_count": 99, "deleted_count": 2, "duration_sec": 1.5}

        db = SimpleNamespace(rebuild_segment_membership=Mock(side_effect=rebuild_segment_membership))
        cfg = SimpleNamespace(
            segment_sql_ring_enabled=True,
            segment_sql_ring_max_per_tick=1,
            segment_sql_statement_timeout_sec=1800,
            segment_sql_page_hits_quiet_only=False,
            segment_sql_min_repeat_sec=0,
            segment_sql_lock_heartbeat_sec=15,
            php_bin="/usr/bin/php",
            mautic_run_as_user="www-data",
        )
        store = SimpleNamespace()
        ring = deque([273])
        done_set: set[int] = set()
        fake_stop = SimpleNamespace(set=Mock())
        fake_thread = SimpleNamespace(join=Mock())

        with (
            patch.object(daemon_mod, "_state_node_id", return_value="node-a"),
            patch.object(daemon_mod, "_segment_sql_try_acquire", return_value=(True, {})),
            patch.object(daemon_mod, "_segment_sql_start_heartbeat", return_value=(fake_stop, fake_thread)),
            patch.object(daemon_mod, "_segment_sql_finish"),
            patch.object(daemon_mod, "_refresh_mautic_segment_count_cache", return_value=True),
        ):
            started_at = time.monotonic()
            launched = _run_sql_segment_ring(
                config=cfg,
                store=store,
                db=db,
                root=root,
                ring=ring,
                rules={273: SQLSegmentRule(segment_id=273, select_sql="SELECT 1 AS lead_id", depends_on=())},
                active_set={273},
                done_set=done_set,
                running={},
                sql_ctx={},
                now_ts=100.0,
                now_local=datetime.now(timezone.utc),
                max_per_tick=1,
                ring_label="segment_sql_long",
                async_worker=True,
            )
            elapsed = time.monotonic() - started_at

            self.assertEqual(launched, 1)
            self.assertLess(elapsed, 0.5)
            self.assertTrue(started.wait(timeout=1.0))
            self.assertTrue(daemon_mod._segment_sql_worker_running(root, 273))
            self.assertEqual(_segment_shared_slots_available({}, root, 1), 0)

            release.set()
            deadline = time.monotonic() + 2.0
            while daemon_mod._segment_sql_worker_running(root, 273) and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(daemon_mod._segment_sql_worker_running(root, 273))
        self.assertIn(273, done_set)
        fake_stop.set.assert_called_once()
        fake_thread.join.assert_called_once_with(timeout=2.0)

    def test_sql_segment_long_ring_respects_persisted_running_lock_after_restart(self) -> None:
        root = "/var/www/site"
        persisted_lock = {
            "root": root,
            "segment_id": 66,
            "status": "running",
            "heartbeat_at": 120.0,
            "orphan_after_sec": 900,
        }
        store = SimpleNamespace(
            list_runtime_sync=Mock(return_value=[("segment_sql_state:abc:66", persisted_lock)])
        )
        db = SimpleNamespace(rebuild_segment_membership=Mock(return_value={}))
        cfg = SimpleNamespace(
            segment_sql_ring_enabled=True,
            segment_sql_ring_max_per_tick=1,
            segment_sql_statement_timeout_sec=1800,
            segment_sql_page_hits_quiet_only=False,
            segment_sql_min_repeat_sec=0,
            segment_sql_orphan_after_sec=900,
            segment_sql_lock_heartbeat_sec=15,
        )

        self.assertEqual(
            _segment_shared_slots_available({}, root, 1, store=store, config=cfg, now_ts=150.0),
            0,
        )

        launched = _run_sql_segment_ring(
            config=cfg,
            store=store,
            db=db,
            root=root,
            ring=deque([70]),
            rules={70: SQLSegmentRule(segment_id=70, select_sql="SELECT 1 AS lead_id", depends_on=())},
            active_set={70},
            done_set=set(),
            running={},
            sql_ctx={},
            now_ts=150.0,
            now_local=datetime.now(timezone.utc),
            max_per_tick=1,
            ring_label="segment_sql_long",
            async_worker=True,
        )

        self.assertEqual(launched, 0)
        db.rebuild_segment_membership.assert_not_called()

    def test_sql_segment_long_ring_respects_live_mysql_rebuild_query_after_timeout(self) -> None:
        root = "/var/www/site"

        class FakeDB:
            rebuild_segment_membership = Mock(return_value={})

            def fetch_processlist(self, *, limit=500):
                return [
                    {
                        "Id": 123,
                        "Command": "Query",
                        "Info": "INSERT IGNORE INTO `mcd_tmp_segment_leads` (`lead_id`) SELECT ...",
                    }
                ]

        db = FakeDB()
        cfg = SimpleNamespace(
            segment_sql_ring_enabled=True,
            segment_sql_ring_max_per_tick=1,
            segment_sql_statement_timeout_sec=1800,
            segment_sql_page_hits_quiet_only=False,
            segment_sql_min_repeat_sec=0,
            segment_sql_orphan_after_sec=900,
            segment_sql_lock_heartbeat_sec=15,
        )
        store = SimpleNamespace(list_runtime_sync=Mock(return_value=[]))
        sql_db_running_count = _segment_sql_active_db_rebuild_query_count(db)

        self.assertEqual(sql_db_running_count, 1)
        self.assertEqual(
            _segment_shared_slots_available(
                {},
                root,
                1,
                store=store,
                config=cfg,
                now_ts=150.0,
                sql_db_running_count=sql_db_running_count,
            ),
            0,
        )

        launched = _run_sql_segment_ring(
            config=cfg,
            store=store,
            db=db,
            root=root,
            ring=deque([76]),
            rules={76: SQLSegmentRule(segment_id=76, select_sql="SELECT 1 AS lead_id", depends_on=())},
            active_set={76},
            done_set=set(),
            running={},
            sql_ctx={},
            now_ts=150.0,
            now_local=datetime.now(timezone.utc),
            max_per_tick=1,
            ring_label="segment_sql_long",
            async_worker=True,
            sql_db_running_count=sql_db_running_count,
        )

        self.assertEqual(launched, 0)
        db.rebuild_segment_membership.assert_not_called()

    def test_sql_segment_plan_preserves_priority_order_without_dependencies(self) -> None:
        rules = {
            66: SQLSegmentRule(segment_id=66, select_sql="SELECT 66 AS lead_id", depends_on=()),
            77: SQLSegmentRule(segment_id=77, select_sql="SELECT 77 AS lead_id", depends_on=()),
            273: SQLSegmentRule(segment_id=273, select_sql="SELECT 273 AS lead_id", depends_on=()),
        }

        self.assertEqual(_plan_sql_segment_ring([273, 77, 66], rules), [273, 77, 66])

    def test_sql_managed_segment_is_removed_from_resume_ring(self) -> None:
        ring = deque([101, 273, 204, 273, 305])

        removed = _remove_ring_entities(ring, {273, 204})

        self.assertEqual(removed, 3)
        self.assertEqual(list(ring), [101, 305])

    def test_campaign_pressure_preserves_configured_segment_throttle(self) -> None:
        root = "/var/www/site"
        cfg = SimpleNamespace(
            segment_throttle_during_campaigns=True,
            campaign_pressure_min_running_sec=120,
            campaign_pressure_min_running_count=2,
        )
        running = {
            _task_key(root, "campaign_trigger", 104): RunningTask(
                row_id=1,
                root=root,
                task_key=_task_key(root, "campaign_trigger", 104),
                task_type="campaign_trigger",
                entity_id=104,
                command_str="campaign trigger 104",
                timeout_sec=3600,
                attempts=1,
                started_at=100.0,
                pid=1004,
            )
        }

        self.assertFalse(
            _campaign_pressure_active(
                cfg,
                running,
                root,
                trigger_prio_ring=deque(),
                trigger_reg_ring=deque(),
                rebuild_prio_ring=deque(),
                rebuild_reg_ring=deque(),
                now_ts=150.0,
            )
        )
        self.assertTrue(
            _campaign_pressure_active(
                cfg,
                running,
                root,
                trigger_prio_ring=deque(),
                trigger_reg_ring=deque(),
                rebuild_prio_ring=deque(),
                rebuild_reg_ring=deque(),
                now_ts=221.0,
            )
        )

    def test_campaign_pressure_can_still_be_disabled(self) -> None:
        cfg = SimpleNamespace(segment_throttle_during_campaigns=False)

        self.assertFalse(
            _campaign_pressure_active(
                cfg,
                {},
                "/var/www/site",
                trigger_prio_ring=deque([104]),
                trigger_reg_ring=deque(),
                rebuild_prio_ring=deque([105]),
                rebuild_reg_ring=deque(),
            )
        )

    def test_segment_whitelist_uses_instance_specific_setting(self) -> None:
        cfg = SimpleNamespace(
            disable_whitelist=False,
            segment_whitelist=[10],
            segment_whitelist_file=None,
            segment_whitelist_instance_settings={
                "site-a.example.com": {"segment_whitelist": [187, "191"]},
                "default": {"segment_whitelist": [5]},
            },
        )
        inst = SimpleNamespace(
            instance_uid="site-a.example.com",
            root="/var/www/site-a",
            name="site-a",
            primary_domain="site-a.example.com",
            domains=["site-a.example.com"],
        )

        self.assertEqual(_segment_whitelist_effective_setting(cfg, inst), {187, 191})

    def test_segment_whitelist_accepts_mcc_host_qualified_instance_uid(self) -> None:
        cfg = SimpleNamespace(
            disable_whitelist=False,
            segment_whitelist=[],
            segment_whitelist_file=None,
            segment_whitelist_instance_settings={
                "site-a.example.com@MauticFarm-02": {"segment_whitelist": [23]},
            },
        )
        inst = SimpleNamespace(
            instance_uid="site-a.example.com",
            root="/var/www/site-a/public_html",
            name="site-a.example.com",
            primary_domain="site-a.example.com",
            domains=["site-a.example.com"],
        )

        self.assertEqual(_segment_whitelist_effective_setting(cfg, inst), {23})

    def test_segment_whitelist_falls_back_to_default_per_instance(self) -> None:
        cfg = SimpleNamespace(
            disable_whitelist=False,
            segment_whitelist=[10],
            segment_whitelist_file=None,
            segment_whitelist_instance_settings={"default": {"segment_whitelist": [5]}},
        )
        inst = SimpleNamespace(
            instance_uid="site-b.example.com",
            root="/var/www/site-b",
            name="site-b",
            primary_domain="site-b.example.com",
            domains=["site-b.example.com"],
        )

        self.assertEqual(_segment_whitelist_effective_setting(cfg, inst), {5})

    def test_segment_whitelist_reads_scoped_common_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/segment-whitelist.txt"
            with open(path, "w", encoding="utf-8") as f:
                f.write("site-a.example.com: 187 191\n")
                f.write("site-b.example.com: 5\n")
                f.write("9\n")
            cfg = SimpleNamespace(
                disable_whitelist=False,
                segment_whitelist=[],
                segment_whitelist_file=path,
                segment_whitelist_instance_settings={},
            )
            inst = SimpleNamespace(
                instance_uid="site-a.example.com",
                root="/var/www/site-a",
                name="site-a",
                primary_domain="site-a.example.com",
                domains=["site-a.example.com"],
            )

            self.assertEqual(_segment_whitelist_effective_setting(cfg, inst), {187, 191})

    def test_segment_whitelist_sync_converts_legacy_file_to_instance_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/segment-whitelist.txt"
            with open(path, "w", encoding="utf-8") as f:
                f.write("187\n191\n")
            cfg = SimpleNamespace(
                disable_whitelist=False,
                segment_whitelist=[],
                segment_whitelist_file=path,
                segment_whitelist_instance_settings={},
            )
            inst = SimpleNamespace(
                instance_uid="ananasmk.sales-snap.com",
                root="/var/www/ananasmk",
                name="ananasmk",
                primary_domain="ananasmk.sales-snap.com",
                domains=["ananasmk.sales-snap.com"],
            )

            self.assertTrue(_sync_segment_whitelist_file(cfg, [inst]))
            with open(path, "r", encoding="utf-8") as f:
                contents = f.read()
            self.assertIn("ananasmk.sales-snap.com: 187 191", contents)
            self.assertNotIn("\n187\n", contents)
            self.assertEqual(_segment_whitelist_effective_setting(cfg, inst), {187, 191})

    def test_published_segment_whitelist_ids_uses_only_integer_ids(self) -> None:
        class FakeDB:
            def __init__(self) -> None:
                self.query = ""
                self.limit = 0
                self.context = {}

            def fetch_ids(self, query_template, limit, context=None):
                self.query = query_template
                self.limit = limit
                self.context = context or {}
                return [187, 191]

        db = FakeDB()

        ids = _published_segment_whitelist_ids(db, {191, 187, -3, 0}, {"root": "/var/www/site"})

        self.assertEqual(ids, [187, 191])
        self.assertEqual(db.limit, 2)
        self.assertIn("ll.is_published = 1", db.query)
        self.assertIn("ll.id IN (187,191)", db.query)
        self.assertEqual(db.context, {"root": "/var/www/site"})

    def test_published_campaign_whitelist_ids_requires_native_publish_window(self) -> None:
        class FakeDB:
            def __init__(self) -> None:
                self.query = ""
                self.limit = 0
                self.context = {}

            def fetch_ids(self, query_template, limit, context=None):
                self.query = query_template
                self.limit = limit
                self.context = context or {}
                return [588]

        db = FakeDB()
        context = {"now_utc": "2026-08-08 07:15:00"}

        ids = _published_campaign_whitelist_ids(db, {588, 558, -3, 0}, context)

        self.assertEqual(ids, [588])
        self.assertEqual(db.limit, 2)
        self.assertIn("c.id IN (558,588)", db.query)
        self.assertIn("c.is_published = 1", db.query)
        self.assertIn("c.publish_up IS NULL OR c.publish_up <= '{now_utc}'", db.query)
        self.assertIn("c.publish_down IS NULL OR c.publish_down >= '{now_utc}'", db.query)
        self.assertEqual(db.context, context)


if __name__ == "__main__":
    unittest.main()
