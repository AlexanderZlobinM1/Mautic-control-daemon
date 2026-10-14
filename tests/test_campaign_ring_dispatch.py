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
from mcd_agent.config import _DEFAULT_SQL_IMPORT_PENDING_COUNT
from mcd_agent.daemon import (
    CampaignWhitelistRealtimeSetting,
    CampaignTriggerProgressSnapshot,
    RunningTask,
    SQLSegmentRule,
    TaskStore,
    _CAMPAIGN_EMAIL_COUNTER_RECONCILE_AT,
    _CAMPAIGN_EMAIL_COUNTER_RECONCILE_DEFER_LOG_AT,
    _CAMPAIGN_NATIVE_FALLBACK_TIMEOUT_SEC,
    _CAMPAIGN_REBUILD_FINISHED_AT,
    _CAMPAIGN_TRIGGER_FUTURE_WAKE,
    _CAMPAIGN_TRIGGER_FUTURE_WAKE_LOGGED,
    _CAMPAIGN_TRIGGER_STUCK_UNTIL,
    _PriorityTaskExecutor,
    _TASK_LOCK_BUSY_RC,
    _campaign_pressure_active,
    _campaign_dispatch_begin,
    _campaign_dispatch_end,
    _campaign_fallback_end,
    _campaign_fallback_try_begin,
    _campaign_native_fallback_last_run_after_completion,
    _campaign_whitelist_realtime_setting,
    _campaign_whitelist_effective_setting,
    _campaign_rebuild_waits_for_trigger,
    _campaign_spill_prefers_priority,
    _campaign_trigger_email_progress_sql,
    _campaign_trigger_event_log_due_exists_sql,
    _campaign_trigger_event_log_progress_sql,
    _campaign_trigger_after_rebuild_pending,
    _campaign_trigger_progress_watchdog,
    _campaign_trigger_should_skip_launch,
    _campaign_trigger_skip_removes_from_ring,
    _campaign_trigger_waits_for_rebuild,
    _classify_import_monitor_row,
    _import_completion_requires_full_scan,
    _ensure_import_pending_cutoff_utc,
    _dispatch_due_campaign_triggers,
    _dispatch_due_campaign_rebuilds,
    _effective_segment_slot_limit,
    _fetch_import_monitor_snapshot,
    _fill_from_ring,
    _fairness_watchdog_dispatch_installs,
    _fairness_promotion_log_due,
    _force_due_campaigns_to_priority,
    _import_pending_poll_due,
    _import_monitor_target_segment_ids,
    _import_target_segment_ids,
    _load_id_file,
    _mark_campaign_rebuild_finished,
    _mark_campaign_trigger_finished,
    _merge_campaign_trigger_audit_ids,
    _monitor_running,
    _move_ring_entities_to_front,
    _plan_sql_segment_ring,
    _pending_import_first_dispatch_installs,
    _published_campaign_whitelist_ids,
    _published_segment_whitelist_ids,
    _priority_campaign_due_check_needed,
    _priority_interleaved_dispatch_installs,
    _priority_realtime_campaign_rebuild_ids,
    _priority_realtime_segment_ids,
    _recover_orphaned_imports_if_safe,
    _remove_ring_entities,
    _rotated_dispatch_installs,
    _root_has_live_campaign_process,
    _run_sql_segment_ring,
    _scheduler_host_running_count,
    _scheduler_host_slots_available,
    _scheduler_instance_slots_available,
    _scheduler_shared_capacity_available,
    _scheduler_pending_roots,
    _scheduler_round_robin_order,
    _set_scheduler_distribution_roots,
    _set_scheduler_fairness_promoted_roots,
    _set_scheduler_priority_pending_roots,
    _take_scheduler_fairness_claimed_roots,
    _segment_sql_active_db_rebuild_query_count,
    _segment_shared_slots_available,
    _segment_task_limit_after_import,
    _segment_whitelist_effective_setting,
    _segment_whitelist_realtime_setting,
    _split_segment_circles,
    _sync_segment_whitelist_file,
    _submit_import_if_segment_slot,
    _task_key,
    _task_execution_lock_key,
    _task_locked_args,
    _task_repeat_interval_sec,
)
from mcd_agent.ring_utils import advance_ring_after_launch


class CampaignRingDispatchTests(unittest.TestCase):
    def test_persisted_cycle_roots_stay_in_shared_fairness_set(self) -> None:
        persisted_root = "/var/www/persisted/public_html"
        ring_root = "/var/www/ring/public_html"
        hidden_root = "/var/www/hidden/public_html"
        installs = [
            SimpleNamespace(root=persisted_root),
            SimpleNamespace(root=ring_root),
            SimpleNamespace(root=hidden_root),
        ]

        pending = _scheduler_pending_roots(
            installs,
            persisted_pending_roots={persisted_root},
            ring_maps=({}, {ring_root: deque([42])}),
        )

        self.assertEqual(pending, {persisted_root, ring_root})

    def test_legacy_priority_alias_merge_preserves_nonempty_without_explicit_clear(self) -> None:
        uid = "electronic@Farm02"
        runtime = {
            "segment_priority_instance_settings": {uid: {"segments": []}},
            "segment_whitelist_instance_settings": {uid: {"segment_whitelist": [23, 86]}},
        }
        merged = daemon_mod._merge_legacy_priority_alias_with_metadata(runtime, {})
        self.assertEqual(merged["segment_priority_instance_settings"][uid]["segment_whitelist"], [23, 86])

        cleared = daemon_mod._merge_legacy_priority_alias_with_metadata(
            runtime,
            {"instances": {uid: {"segment_priority_instance_settings": {"tombstone": True}}}},
        )
        self.assertEqual(cleared["segment_priority_instance_settings"][uid], {"segments": []})

    def test_legacy_priority_alias_merge_deduplicates_nonempty_rows(self) -> None:
        uid = "prodajadelova@Farm02"
        merged = daemon_mod._merge_legacy_priority_alias_with_metadata(
            {
                "segment_priority_instance_settings": {uid: {"segments": [23, 44]}},
                "segment_whitelist_instance_settings": {uid: {"segment_whitelist": [44, 61, 55]}},
            },
            {},
        )
        self.assertEqual(merged["segment_priority_instance_settings"][uid], {"segments": [44, 61, 55, 23]})

    def test_restart_reconciles_and_adopts_live_persisted_worker(self) -> None:
        class Store:
            def sync_sqlite_running_shadow(self) -> dict[str, int]:
                return {"replaced": 0}

            def running_rows(self) -> list[dict[str, object]]:
                return [
                    {
                        "id": 7,
                        "root": "/var/www/mautic",
                        "task_key": "/var/www/mautic|segment|108",
                        "task_type": "segment",
                        "entity_id": 108,
                        "command_str": "php /var/www/mautic/bin/console mautic:segments:update -i 108",
                        "pid": 4321,
                        "timeout_sec": 3600,
                        "attempts": 1,
                        "manual_request_id": None,
                        "started_at": time.time() - 120,
                    }
                ]

            def finish(self, *args: object, **kwargs: object) -> None:
                raise AssertionError(f"live worker was incorrectly finished: {args} {kwargs}")

        with patch.object(daemon_mod, "_is_pid_alive", return_value=True), patch.object(
            daemon_mod, "_pid_matches_task_command", return_value=True
        ):
            running: dict[str, RunningTask] = {}
            stats = daemon_mod._reconcile_running_state(store=Store(), running=running, popens={})

        self.assertEqual(stats["adopted"], 1)
        self.assertIn("/var/www/mautic|segment|108", running)
        self.assertEqual(running["/var/www/mautic|segment|108"].pid, 4321)

    def tearDown(self) -> None:
        _CAMPAIGN_TRIGGER_STUCK_UNTIL.clear()
        _CAMPAIGN_TRIGGER_FUTURE_WAKE.clear()
        _CAMPAIGN_TRIGGER_FUTURE_WAKE_LOGGED.clear()
        _CAMPAIGN_EMAIL_COUNTER_RECONCILE_AT.clear()
        _CAMPAIGN_EMAIL_COUNTER_RECONCILE_DEFER_LOG_AT.clear()
        _CAMPAIGN_REBUILD_FINISHED_AT.clear()
        daemon_mod._ENTITY_LAUNCH_GUARD.clear()
        with daemon_mod._SEGMENT_SQL_WORKERS_LOCK:
            daemon_mod._SEGMENT_SQL_WORKERS.clear()
        with daemon_mod._CAMPAIGN_FALLBACK_COORDINATOR_LOCK:
            daemon_mod._CAMPAIGN_FALLBACK_ACTIVE_ROOTS.clear()
            daemon_mod._CAMPAIGN_DISPATCHING_ROOTS.clear()
        _set_scheduler_priority_pending_roots(set())
        _set_scheduler_fairness_promoted_roots(set())
        daemon_mod._set_scheduler_important_pending_roots(set())
        _set_scheduler_distribution_roots([], worker_slots=1)
        _take_scheduler_fairness_claimed_roots()
        with daemon_mod._SCHEDULER_FAIRNESS_LOCK:
            daemon_mod._SCHEDULER_BASELINE_LAST_LAUNCH_BY_LANE.clear()

    def test_missing_optional_campaign_whitelist_file_is_quiet(self) -> None:
        missing = "/tmp/mcd-missing-campaign-whitelist"

        with patch("mcd_agent.daemon.logging.warning") as warning:
            self.assertEqual(_load_id_file(missing), set())

        warning.assert_not_called()

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

    def test_shared_round_robin_first_pass_is_one_worker_per_ready_root(self) -> None:
        roots = [f"/var/www/root-{index}" for index in range(40)]
        first_pass = _scheduler_round_robin_order(roots, 12)
        self.assertEqual(len(first_pass), 12)
        self.assertEqual(len(set(first_pass)), 12)

        fewer_roots = _scheduler_round_robin_order(roots[:3], 12)
        self.assertEqual(len(fewer_roots), 12)
        self.assertEqual(set(fewer_roots), set(roots[:3]))

        _set_scheduler_distribution_roots(roots, worker_slots=12)
        admitted: list[str] = []
        for _slot in range(12):
            for root in roots:
                if daemon_mod._scheduler_shared_admission_allowed(root, "segment"):
                    daemon_mod._mark_scheduler_shared_admission(root, "segment")
                    admitted.append(root)
                    break
        self.assertEqual(len(admitted), 12)
        self.assertEqual(len(set(admitted)), 12)
        _set_scheduler_distribution_roots([], worker_slots=1)

    def test_super_priority_is_outside_shared_host_and_instance_caps(self) -> None:
        root = "/var/www/super"
        cfg = SimpleNamespace(
            scheduler_host_max_parallel=1,
            scheduler_instance_max_parallel=1,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=0,
            segment_mode="id_weighted",
            segment_priority_parallel_idle=1,
            segment_regular_parallel_idle=1,
        )
        running = {
            "ordinary": SimpleNamespace(root="/var/www/ordinary", task_type="segment", entity_id=1),
            "super": SimpleNamespace(root=root, task_type="segment_super_priority", entity_id=58),
        }
        self.assertEqual(_scheduler_host_slots_available(cfg, running, "segment"), 0)
        self.assertIsNone(_scheduler_host_slots_available(cfg, running, "segment_super_priority", root=root))
        self.assertIsNone(
            _scheduler_instance_slots_available(
                cfg,
                running,
                root=root,
                task_type="segment_super_priority",
            )
        )

    def test_local_instance_full_yields_when_shared_host_slot_is_free(self) -> None:
        blocked_root = "/var/www/blocked"
        cfg = SimpleNamespace(
            scheduler_host_max_parallel=2,
            scheduler_instance_max_parallel=1,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=0,
        )
        running = {
            "blocked-parent": SimpleNamespace(root=blocked_root, task_type="segment", entity_id=230),
        }

        self.assertEqual(
            _scheduler_instance_slots_available(
                cfg,
                running,
                root=blocked_root,
                task_type="segment",
            ),
            0,
        )
        self.assertEqual(
            _scheduler_host_slots_available(cfg, running, "segment", root=blocked_root),
            1,
        )
        self.assertTrue(
            _scheduler_shared_capacity_available(
                cfg,
                running,
                root=blocked_root,
            )
        )

        running["other-root"] = SimpleNamespace(root="/var/www/other", task_type="segment", entity_id=1)
        self.assertFalse(
            _scheduler_shared_capacity_available(
                cfg,
                running,
                root=blocked_root,
            )
        )

    def test_super_lane_owner_overrides_generic_adopted_wrapper(self) -> None:
        root = "/var/www/super"
        super_key = _task_key(root, "segment_super_priority", 58)
        executor = _PriorityTaskExecutor()
        with executor._guard:
            executor._active[super_key] = (root, "segment_super_priority", "segment_super_priority")
        wrapper = {
            "observed-wrapper": SimpleNamespace(
                root=root,
                task_type="segment",
                entity_id=58,
            )
        }
        self.assertEqual(
            daemon_mod._scheduler_actual_work_count(wrapper, priority_executor=executor),
            0,
        )

        ordinary = {
            "ordinary": SimpleNamespace(root=root, task_type="segment", entity_id=59),
            "wrapper": SimpleNamespace(root=root, task_type="segment", entity_id=59),
        }
        self.assertEqual(
            daemon_mod._scheduler_actual_work_count(ordinary),
            1,
        )

    def test_distribution_gate_serves_unassigned_roots_before_second_slot(self) -> None:
        roots = ["/var/www/a", "/var/www/b", "/var/www/c"]
        _set_scheduler_distribution_roots(roots, worker_slots=12)
        self.assertTrue(daemon_mod._scheduler_shared_admission_allowed(roots[0], "segment"))
        daemon_mod._mark_scheduler_shared_admission(roots[0], "segment")
        self.assertFalse(daemon_mod._scheduler_shared_admission_allowed(roots[0], "segment"))
        self.assertTrue(daemon_mod._scheduler_shared_admission_allowed(roots[1], "segment"))
        _set_scheduler_distribution_roots([], worker_slots=1)

    def test_campaign_and_plugin_admission_bypass_segment_rr_without_consuming_turn(self) -> None:
        roots = ["/var/www/a", "/var/www/b"]
        _set_scheduler_distribution_roots(roots, worker_slots=2, cursor=0)
        daemon_mod._mark_scheduler_shared_admission(roots[0], "segment")

        # Ordinary campaigns and every plugin-operation task use their own
        # lanes/caps. A served segment root must not starve them, and their
        # admissions must not consume the segment root's fair-turn receipt.
        for task_type in (
            "campaign_trigger",
            "campaign_rebuild",
            "campaign_update",
            "campaign_native_fallback",
            "job:plugin_operation:woocommerce-sync",
            "job:plugin_operation_bootstrap:woocommerce-sync",
        ):
            self.assertTrue(daemon_mod._scheduler_shared_admission_allowed(roots[0], task_type))
            daemon_mod._mark_scheduler_shared_admission(roots[0], task_type)

        self.assertFalse(daemon_mod._scheduler_shared_admission_allowed(roots[0], "segment"))
        self.assertTrue(daemon_mod._scheduler_shared_admission_allowed(roots[1], "segment"))
        _set_scheduler_distribution_roots([], worker_slots=1)

    def test_distribution_round_reopens_free_capacity_after_full_root_scan(self) -> None:
        roots = ["/var/www/slow", "/var/www/ready"]
        _set_scheduler_distribution_roots(roots, worker_slots=2, cursor=0)

        # The ready root has already taken its first fair turn. The slow root
        # was not dispatchable in this pass, so its unserved receipt must not
        # strand the second shared worker in the next pass.
        _mark = daemon_mod._mark_scheduler_shared_admission
        _mark(roots[1], "segment")
        self.assertFalse(daemon_mod._scheduler_shared_admission_allowed(roots[1], "segment"))
        daemon_mod._mark_scheduler_shared_admission_skip(roots[0], "segment")
        self.assertTrue(daemon_mod._scheduler_shared_admission_allowed(roots[1], "segment"))
        _set_scheduler_distribution_roots([], worker_slots=1)

    def test_interleaved_main_and_refill_scans_do_not_clear_each_other(self) -> None:
        roots = ["/var/www/a", "/var/www/b", "/var/www/c"]
        _set_scheduler_distribution_roots(roots, worker_slots=3, cursor=0)

        daemon_mod._mark_scheduler_shared_admission(roots[0], "segment")

        def refill_scan() -> None:
            daemon_mod._mark_scheduler_shared_admission(roots[1], "segment")
            daemon_mod._mark_scheduler_shared_admission_skip(roots[2], "segment")

        refill_thread = threading.Thread(target=refill_scan)
        refill_thread.start()
        refill_thread.join()

        # Main and refill share one durable served-root receipt. Once every
        # eligible root has a claim/skip receipt, the next ordinary turn can
        # reopen without a process-wide active-scan counter.
        self.assertTrue(daemon_mod._scheduler_shared_admission_allowed(roots[0], "segment"))
        _set_scheduler_distribution_roots([], worker_slots=1)

    def test_distribution_skip_completes_unlaunchable_ordinary_root_turn(self) -> None:
        roots = ["/var/www/hot", "/var/www/blocked"]
        _set_scheduler_distribution_roots(roots, worker_slots=2, cursor=0)
        daemon_mod._mark_scheduler_shared_admission(roots[0], "segment")

        self.assertFalse(daemon_mod._scheduler_shared_admission_allowed(roots[0], "segment"))
        daemon_mod._mark_scheduler_shared_admission_skip(roots[1], "segment")
        self.assertTrue(daemon_mod._scheduler_shared_admission_allowed(roots[0], "segment"))

        # Static super-priority ticks are independent of the ordinary round.
        daemon_mod._set_scheduler_distribution_roots(roots, worker_slots=2, cursor=0)
        daemon_mod._mark_scheduler_shared_admission(roots[0], "segment")
        daemon_mod._mark_scheduler_shared_admission_skip(roots[1], "segment_super_priority")
        self.assertFalse(daemon_mod._scheduler_shared_admission_allowed(roots[0], "segment"))
        self.assertTrue(daemon_mod._scheduler_shared_admission_allowed(roots[1], "segment_super_priority"))
        _set_scheduler_distribution_roots([], worker_slots=1)

    def test_distribution_gate_keeps_served_roots_when_input_order_rotates(self) -> None:
        roots = ["/var/www/a", "/var/www/b", "/var/www/c"]
        _set_scheduler_distribution_roots(roots, worker_slots=3, cursor=0)
        daemon_mod._mark_scheduler_shared_admission(roots[0], "segment")

        # The outer dispatch loop rotates its instance list between ticks. A
        # caller-order change is not a new fairness round and must not make
        # root A eligible for a second claim before B/C receive their turn.
        _set_scheduler_distribution_roots([roots[1], roots[2], roots[0]], worker_slots=3)

        self.assertEqual(daemon_mod._SCHEDULER_DISTRIBUTION_READY_ROOTS, tuple(roots))
        self.assertFalse(daemon_mod._scheduler_shared_admission_allowed(roots[0], "segment"))
        self.assertTrue(daemon_mod._scheduler_shared_admission_allowed(roots[1], "segment"))
        _set_scheduler_distribution_roots([], worker_slots=1)

    def test_distribution_gate_keeps_served_roots_when_ready_set_changes(self) -> None:
        roots = ["/var/www/a", "/var/www/b", "/var/www/c"]
        _set_scheduler_distribution_roots(roots, worker_slots=3, cursor=0)
        daemon_mod._mark_scheduler_shared_admission(roots[0], "segment")
        daemon_mod._mark_scheduler_shared_admission(roots[1], "segment")

        _set_scheduler_distribution_roots([roots[1], roots[2], "/var/www/d"], worker_slots=3)

        self.assertFalse(daemon_mod._scheduler_shared_admission_allowed(roots[1], "segment"))
        self.assertTrue(daemon_mod._scheduler_shared_admission_allowed(roots[2], "segment"))
        self.assertTrue(daemon_mod._scheduler_shared_admission_allowed("/var/www/d", "segment"))
        _set_scheduler_distribution_roots([], worker_slots=1)

    def test_failed_submit_does_not_consume_a_served_root_turn(self) -> None:
        roots = ["/var/www/hot", "/var/www/cold"]
        _set_scheduler_distribution_roots(roots, worker_slots=1, cursor=0)
        config = SimpleNamespace(command_timeout_sec=30)
        ring = deque([101])

        class EmptyStore:
            @staticmethod
            def get_runtime_sync(_key: str) -> None:
                return None

        with patch.object(daemon_mod, "_submit_if_slot", return_value=False):
            launched = _fill_from_ring(
                ring=ring,
                ring_limit=1,
                total_limit=1,
                root=roots[0],
                task_type="segment",
                running={},
                ring_entities={101},
                config=config,
                store=EmptyStore(),
                popens={},
                build_args=lambda sid: ["php", "segments:update", str(sid)],
                admission_claim=lambda _sid: True,
                admission_release=lambda _sid: None,
                bypass_repeat_guard_entities={101},
                bypass_baseline_admission=True,
                max_launches=1,
            )

        self.assertEqual(launched, 0)
        self.assertTrue(daemon_mod._scheduler_shared_admission_allowed(roots[0], "segment"))
        self.assertTrue(daemon_mod._scheduler_shared_admission_allowed(roots[1], "segment"))
        _set_scheduler_distribution_roots([], worker_slots=1)

    def test_remaining_root_uses_free_host_slot_when_important_root_is_instance_full(self) -> None:
        important_root = "/var/www/important"
        remaining_root = "/var/www/remaining"
        cycle_key = daemon_mod._segment_cycle_key(remaining_root)

        class Store:
            def get_runtime_sync(self, key: str) -> dict[str, object] | None:
                if key == cycle_key:
                    return {
                        "schema": "mcd-segment-cycle-v1",
                        "status": "active",
                        "items": [200],
                        "important_items": [],
                        "remaining_items": [200],
                        "claimed": [],
                        "running": [],
                        "done": [],
                        "errors": [],
                    }
                return None

            def put_runtime_sync(self, _key: str, _payload: dict[str, object]) -> None:
                return None

        cfg = SimpleNamespace(
            command_timeout_sec=30,
            scheduler_host_max_parallel=2,
            scheduler_instance_max_parallel=1,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=0,
        )
        running = {
            "important-worker": SimpleNamespace(
                root=important_root,
                task_type="segment",
                entity_id=101,
            )
        }
        daemon_mod._set_scheduler_important_pending_roots({important_root})
        first_ring = deque([200])
        with patch.object(daemon_mod, "_submit_if_slot", return_value=True) as submit:
            launched = _fill_from_ring(
                ring=first_ring,
                ring_limit=1,
                total_limit=1,
                root=remaining_root,
                task_type="segment",
                running=running,
                ring_entities={200},
                config=cfg,
                store=Store(),
                popens={},
                build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
                admission_claim=lambda _sid: True,
                remove_on_launch=True,
                bypass_baseline_admission=True,
                max_launches=1,
            )

        self.assertEqual(launched, 1)
        submit.assert_called_once()

        # Once the important root is actually free, the same remaining item
        # must wait behind it again; the capacity filter is not a tier bypass.
        running.clear()
        second_ring = deque([200])
        with patch.object(daemon_mod, "_submit_if_slot", return_value=True) as submit:
            launched = _fill_from_ring(
                ring=second_ring,
                ring_limit=1,
                total_limit=1,
                root=remaining_root,
                task_type="segment",
                running=running,
                ring_entities={200},
                config=cfg,
                store=Store(),
                popens={},
                build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
                admission_claim=lambda _sid: True,
                remove_on_launch=True,
                bypass_baseline_admission=True,
                max_launches=1,
            )

        self.assertEqual(launched, 0)
        submit.assert_not_called()

    def test_remaining_root_ignores_capacity_only_important_root_with_no_launchable_owner(self) -> None:
        important_root = "/var/www/mcd713php83_sandbox/public_html"
        remaining_root = "/var/www/b2bss/public_html"
        important_cycle_key = daemon_mod._segment_cycle_key(important_root)
        remaining_cycle_key = daemon_mod._segment_cycle_key(remaining_root)
        important_context_key = daemon_mod._segment_dispatch_context_key(important_root)

        class Store:
            def __init__(self) -> None:
                self.payloads = {
                    important_cycle_key: {
                        "schema": "mcd-segment-cycle-v1",
                        "status": "active",
                        "items": [19, 21, 3, 4, 5, 6, 7, 20, 22],
                        "important_items": [19, 21],
                        "remaining_items": [3, 4, 5, 6, 7, 20, 22],
                        "claimed": [],
                        "running": [],
                        "done": [3, 4, 5],
                        "errors": [],
                    },
                    important_context_key: {
                        "schema": "mcd-segment-dispatch-context-v1",
                        "mautic_major": 7,
                        "parents_by_child": {"20": [19], "21": [19], "22": [21]},
                        "children_by_parent": {"19": [20, 21], "21": [22]},
                        "blocked_ids": [],
                        "static_configured_ids": [],
                        "static_owned_ids": [],
                        "static_active_ids": [],
                    },
                    remaining_cycle_key: {
                        "schema": "mcd-segment-cycle-v1",
                        "status": "active",
                        "items": [200],
                        "important_items": [],
                        "remaining_items": [200],
                        "claimed": [],
                        "running": [],
                        "done": [],
                        "errors": [],
                    },
                }

            def get_runtime_sync(self, key: str) -> dict[str, object] | None:
                return self.payloads.get(key)

            def put_runtime_sync(self, key: str, payload: dict[str, object]) -> None:
                self.payloads[key] = payload

        cfg = SimpleNamespace(
            command_timeout_sec=30,
            scheduler_host_max_parallel=12,
            scheduler_instance_max_parallel=6,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=1,
        )
        store = Store()
        daemon_mod._set_scheduler_important_pending_roots({important_root})

        # The important IDs are pending, but their Mautic 7 component has
        # native leaf owners 20/22.  Capacity-only gating must not turn this
        # unresolved root into a global barrier for b2bss's ready work.
        self.assertFalse(
            daemon_mod._scheduler_important_root_dispatchable(
                cfg,
                {},
                important_root,
                store=store,
            )
        )
        self.assertTrue(
            daemon_mod._scheduler_segment_entity_allowed(
                store,
                remaining_root,
                200,
                config=cfg,
                running={},
            )
        )

    def test_selected_profile_fanout_is_not_clamped_without_explicit_host_cap(self) -> None:
        root = "/var/www/site"
        cfg = SimpleNamespace(
            profile_name="midi",
            scheduler_host_max_parallel=0,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=1,
            segment_mode="id_weighted",
            segment_priority_parallel_idle=3,
            segment_regular_parallel_idle=1,
            scheduler_instance_max_parallel=0,
        )
        running = {
            f"segment-{idx}": SimpleNamespace(root=root, task_type="segment", entity_id=idx)
            for idx in range(4)
        }

        self.assertIsNone(_scheduler_host_slots_available(cfg, running, "segment", root=root))
        self.assertIsNone(_scheduler_instance_slots_available(cfg, running, root=root, task_type="segment"))

    def test_explicit_host_safety_cap_still_clamps_profile_fanout(self) -> None:
        root = "/var/www/site"
        cfg = SimpleNamespace(
            profile_name="hiload",
            scheduler_host_max_parallel=2,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=0,
            segment_mode="id_weighted",
            segment_priority_parallel_idle=6,
            segment_regular_parallel_idle=2,
        )
        running = {
            "segment-baseline": SimpleNamespace(root=root, task_type="segment", entity_id=1)
        }

        self.assertEqual(_scheduler_host_slots_available(cfg, running, "segment", root=root), 1)

    def test_elastic_host_budget_counts_baselines_inside_hard_host_cap(self) -> None:
        cfg = SimpleNamespace(
            scheduler_host_max_parallel=6,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=1,
            segment_mode="id_weighted",
            segment_priority_parallel_idle=3,
            segment_regular_parallel_idle=1,
            campaign_total_parallel=2,
        )
        prodajadelova = "/var/www/prodajadelova/public_html"
        five_segments = {
            f"segment-{idx}": SimpleNamespace(root=f"/var/www/segment-{idx}", task_type="segment")
            for idx in range(5)
        }
        six_campaigns = {
            f"campaign-{campaign_id}": SimpleNamespace(
                root=prodajadelova,
                task_type="campaign_rebuild",
                entity_id=campaign_id,
            )
            for campaign_id in range(6)
        }

        self.assertEqual(_scheduler_host_slots_available(cfg, five_segments, "segment"), 0)
        self.assertEqual(_scheduler_host_slots_available(cfg, five_segments, "campaign_rebuild"), 1)
        self.assertEqual(_scheduler_host_slots_available(cfg, five_segments, "import"), 1)
        self.assertEqual(_scheduler_host_slots_available(cfg, six_campaigns, "campaign_rebuild"), 0)
        self.assertEqual(_scheduler_host_slots_available(cfg, six_campaigns, "segment"), 0)

    def test_farm_instance_limit_reserves_root_slot_when_priority_work_is_pending(self) -> None:
        root = "/var/www/mensa/public_html"
        cfg = SimpleNamespace(scheduler_instance_max_parallel=3)
        two_segments = {
            f"segment-{idx}": SimpleNamespace(root=root, task_type="segment")
            for idx in range(2)
        }
        _set_scheduler_priority_pending_roots({root})

        self.assertEqual(
            _scheduler_instance_slots_available(cfg, two_segments, root=root, task_type="segment"),
            0,
        )
        self.assertEqual(
            _scheduler_instance_slots_available(cfg, two_segments, root=root, task_type="campaign_trigger"),
            1,
        )

    def test_host_fairness_reserves_next_slot_for_promoted_root(self) -> None:
        starving_root = "/var/www/prodajadelova/public_html"
        ordinary_root = "/var/www/ordinary/public_html"
        cfg = SimpleNamespace(
            scheduler_host_max_parallel=1,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=0,
        )
        running = {}
        _set_scheduler_fairness_promoted_roots({starving_root})

        self.assertEqual(
            _scheduler_host_slots_available(cfg, running, "segment", root=ordinary_root),
            0,
        )
        self.assertEqual(
            _scheduler_host_slots_available(cfg, running, "segment", root=starving_root),
            1,
        )

    def test_host_fairness_advances_after_promoted_root_claims_slot(self) -> None:
        first_root = "/var/www/first/public_html"
        second_root = "/var/www/second/public_html"
        cfg = SimpleNamespace(
            scheduler_host_max_parallel=1,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=0,
        )
        running = {}
        _set_scheduler_fairness_promoted_roots([first_root, second_root])

        self.assertEqual(_scheduler_host_slots_available(cfg, running, "segment", root=first_root), 1)
        self.assertEqual(_scheduler_host_slots_available(cfg, running, "segment", root=second_root), 0)
        daemon_mod._mark_scheduler_fairness_claim(first_root, "segment")
        running["first-baseline"] = SimpleNamespace(root=first_root, task_type="segment")

        self.assertEqual(_take_scheduler_fairness_claimed_roots(), {first_root})
        running.pop("first-baseline")
        _set_scheduler_fairness_promoted_roots([second_root])
        self.assertEqual(_scheduler_host_slots_available(cfg, running, "segment", root=second_root), 1)

    def test_many_promoted_roots_cannot_bypass_hard_host_cap(self) -> None:
        cfg = SimpleNamespace(
            scheduler_host_max_parallel=3,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=0,
        )
        running = {
            "owner-baseline": SimpleNamespace(root="/promoted-0", task_type="segment"),
            "ordinary-baseline": SimpleNamespace(root="/ordinary", task_type="segment"),
            "a": SimpleNamespace(root="/a", task_type="import"),
            "a-extra-1": SimpleNamespace(root="/a", task_type="import"),
            "a-extra-2": SimpleNamespace(root="/a", task_type="import"),
        }
        roots = [f"/promoted-{idx}" for idx in range(20)]
        _set_scheduler_fairness_promoted_roots(roots)

        self.assertEqual(_scheduler_host_slots_available(cfg, running, "segment", root=roots[0]), 0)
        self.assertEqual(_scheduler_host_slots_available(cfg, running, "segment", root="/ordinary"), 0)

    def test_three_ready_roots_share_aggregate_host_and_instance_capacity(self) -> None:
        cfg = SimpleNamespace(
            scheduler_host_max_parallel=3,
            scheduler_instance_max_parallel=1,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=0,
        )
        roots = [f"/ready-{idx}" for idx in range(3)]
        running = {}
        _set_scheduler_fairness_promoted_roots(set())

        for idx, root in enumerate(roots):
            self.assertEqual(
                _scheduler_host_slots_available(cfg, running, "segment", root=root),
                3 - idx,
            )
            self.assertEqual(
                _scheduler_instance_slots_available(cfg, running, root=root, task_type="segment"),
                1,
            )
            running[f"segment-{idx}"] = SimpleNamespace(root=root, task_type="segment")

        self.assertEqual(len(running), cfg.scheduler_host_max_parallel)
        self.assertEqual(_scheduler_host_slots_available(cfg, running, "segment", root="/ready-3"), 0)
        self.assertEqual(
            _scheduler_instance_slots_available(cfg, running, root=roots[0], task_type="segment"),
            0,
        )

    def test_explicit_host_cap_counts_normal_static_campaign_import_once(self) -> None:
        cfg = SimpleNamespace(
            scheduler_host_max_parallel=4,
            scheduler_instance_max_parallel=1,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=0,
        )
        normal_root = "/normal"
        static_root = "/static"
        campaign_root = "/campaign"
        import_root = "/import"
        static_key = _task_key(static_root, "segment_priority", 101)
        campaign_key = _task_key(campaign_root, "campaign_trigger", 202)
        running = {
            _task_key(normal_root, "segment", 1): SimpleNamespace(
                root=normal_root,
                task_type="segment",
                entity_id=1,
            ),
            _task_key(import_root, "import", None): SimpleNamespace(
                root=import_root,
                task_type="import",
                entity_id=None,
            ),
            # This adopted/static wrapper has the same logical segment as the
            # active priority executor item and must not be counted twice.
            static_key: SimpleNamespace(
                root=static_root,
                task_type="segment",
                entity_id=101,
            ),
        }
        executor = _PriorityTaskExecutor()
        with executor._guard:
            executor._active[static_key] = (static_root, "segment_priority", "segment_priority")
            executor._active[campaign_key] = (campaign_root, "campaign_trigger", "campaign_trigger")

        previous_executor = daemon_mod._ACTIVE_PRIORITY_EXECUTOR
        daemon_mod._ACTIVE_PRIORITY_EXECUTOR = executor
        try:
            self.assertEqual(daemon_mod._scheduler_actual_work_count(running), 4)
            self.assertEqual(_scheduler_host_running_count(running, "segment"), 3)
            self.assertEqual(_scheduler_host_running_count(running, "campaign_trigger"), 1)
            self.assertEqual(
                _scheduler_host_slots_available(cfg, running, "campaign_rebuild", root="/ready"),
                0,
            )
            self.assertEqual(
                _scheduler_instance_slots_available(cfg, running, root=static_root, task_type="segment_priority"),
                0,
            )
            self.assertEqual(
                _scheduler_instance_slots_available(cfg, running, root=import_root, task_type="import"),
                0,
            )
            self.assertEqual(
                _scheduler_instance_slots_available(cfg, running, root="/ready", task_type="segment"),
                1,
            )
            with patch.object(daemon_mod, "_spawn_command") as spawn:
                self.assertFalse(
                    executor.launch(
                        cfg,
                        root="/ready",
                        task_type="segment_priority",
                        entity_id=303,
                        running=running,
                        args=["php", "bin/console"],
                        interval_sec=1,
                        max_parallel=1,
                    )
                )
                spawn.assert_not_called()
        finally:
            daemon_mod._ACTIVE_PRIORITY_EXECUTOR = previous_executor

    def test_explicit_host_cap_blocks_root_baselines_when_host_is_full(self) -> None:
        root = "/var/www/prodajadelova/public_html"
        cfg = SimpleNamespace(
            scheduler_host_max_parallel=1,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=0,
            scheduler_instance_max_parallel=1,
        )
        running = {
            "shared": SimpleNamespace(root="/other", task_type="import"),
        }

        self.assertEqual(_scheduler_host_slots_available(cfg, running, "segment", root=root), 0)
        self.assertEqual(_scheduler_host_slots_available(cfg, running, "campaign_trigger", root=root), 0)
        self.assertEqual(_scheduler_instance_slots_available(cfg, running, root=root, task_type="segment"), 1)
        self.assertEqual(_scheduler_instance_slots_available(cfg, running, root=root, task_type="campaign_rebuild"), 1)
        self.assertEqual(_scheduler_host_slots_available(cfg, running, "import", root=root), 0)

    def test_explicit_instance_cap_counts_baseline_tasks(self) -> None:
        root = "/var/www/prodajadelova/public_html"
        cfg = SimpleNamespace(scheduler_instance_max_parallel=1)
        running = {
            "segment-baseline": SimpleNamespace(root=root, task_type="segment"),
        }

        self.assertEqual(_scheduler_instance_slots_available(cfg, running, root=root, task_type="segment"), 0)
        self.assertEqual(
            _scheduler_instance_slots_available(cfg, running, root=root, task_type="campaign_trigger"),
            0,
        )

    def test_additional_lightweight_work_uses_shared_pool(self) -> None:
        root = "/var/www/prodajadelova/public_html"
        cfg = SimpleNamespace(
            scheduler_host_max_parallel=1,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=0,
        )
        running = {
            "segment-baseline": SimpleNamespace(root=root, task_type="segment"),
            "segment-extra": SimpleNamespace(root=root, task_type="import"),
            "campaign-baseline": SimpleNamespace(root=root, task_type="campaign_trigger"),
        }

        self.assertEqual(_scheduler_host_slots_available(cfg, running, "segment", root=root), 0)
        self.assertEqual(_scheduler_host_slots_available(cfg, running, "campaign_rebuild", root=root), 0)

    def test_import_owns_segment_baseline_before_segment_work(self) -> None:
        root = "/var/www/prodajadelova/public_html"
        cfg = SimpleNamespace(
            scheduler_host_max_parallel=1,
            scheduler_elastic_slots_enabled=True,
            scheduler_emergency_reserved_slots=0,
        )
        running = {
            "root-import-baseline": SimpleNamespace(root=root, task_type="import"),
            "other-import-baseline": SimpleNamespace(root="/other", task_type="import"),
            "other-import-extra": SimpleNamespace(root="/other", task_type="import"),
        }

        self.assertEqual(_scheduler_host_slots_available(cfg, running, "segment", root=root), 0)
        self.assertEqual(_segment_task_limit_after_import(running, root, 1), 0)

    def test_baseline_admissions_are_staggered_per_lane(self) -> None:
        self.assertTrue(daemon_mod._scheduler_baseline_launch_due("segment", 100.0))
        daemon_mod._mark_scheduler_baseline_launch("segment", 100.0)

        self.assertFalse(daemon_mod._scheduler_baseline_launch_due("segment", 104.9))
        self.assertTrue(daemon_mod._scheduler_baseline_launch_due("segment", 105.0))
        self.assertTrue(daemon_mod._scheduler_baseline_launch_due("campaign", 100.0))

    def test_dispatch_rotation_changes_first_instance_each_tick(self) -> None:
        installs = ["a", "b", "c"]

        self.assertEqual(_rotated_dispatch_installs(installs, 0), ["a", "b", "c"])
        self.assertEqual(_rotated_dispatch_installs(installs, 1), ["b", "c", "a"])
        self.assertEqual(_rotated_dispatch_installs(installs, 4), ["b", "c", "a"])

    def test_priority_dispatch_visits_each_instance_once_per_cycle(self) -> None:
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
            "/regular-2", "/regular-3", "/regular-4",
        ])

    def test_fairness_watchdog_promotes_an_overdue_root_and_deduplicates_input(self) -> None:
        a = SimpleNamespace(root="/a")
        b = SimpleNamespace(root="/b")
        pending_since = {"/b": 100.0}

        planned, promoted = _fairness_watchdog_dispatch_installs(
            [a, a, b],
            pending_roots={"/b"},
            pending_since_by_root=pending_since,
            now_ts=500.0,
            watchdog_sec=300,
        )

        self.assertEqual([inst.root for inst in planned], ["/b", "/a"])
        self.assertEqual(promoted, ["/b"])

    def test_fairness_promotion_log_ignores_ring_reordering_until_hourly_refresh(self) -> None:
        previous = frozenset({"/a", "/b"})

        self.assertFalse(
            _fairness_promotion_log_due(
                ["/b", "/a"],
                previous,
                now_ts=200.0,
                next_log_at=3600.0,
            )
        )
        self.assertTrue(
            _fairness_promotion_log_due(
                ["/b", "/a"],
                previous,
                now_ts=3600.0,
                next_log_at=3600.0,
            )
        )
        self.assertTrue(
            _fairness_promotion_log_due(
                ["/a"],
                frozenset(),
                now_ts=1.0,
                next_log_at=0.0,
            )
        )
        self.assertFalse(
            _fairness_promotion_log_due(
                [],
                previous,
                now_ts=3600.0,
                next_log_at=3600.0,
            )
        )

    def test_pending_import_precedes_whitelist_segments_across_instances(self) -> None:
        electronic = SimpleNamespace(root="/var/www/electronic/public_html")
        mensa = SimpleNamespace(root="/var/www/mensa/public_html")
        other = SimpleNamespace(root="/var/www/other/public_html")

        planned = _pending_import_first_dispatch_installs(
            [electronic, other, electronic, mensa],
            {mensa.root: 1},
        )

        self.assertEqual(planned[0].root, mensa.root)
        self.assertEqual([inst.root for inst in planned[1:]], [
            electronic.root,
            other.root,
            electronic.root,
        ])

    def test_import_priority_preserves_rotation_when_no_import_is_pending(self) -> None:
        installs = [
            SimpleNamespace(root="/var/www/a"),
            SimpleNamespace(root="/var/www/b"),
        ]

        self.assertEqual(
            _pending_import_first_dispatch_installs(installs, {"/var/www/a": 0}),
            installs,
        )

    def test_campaign_rebuild_and_trigger_share_execution_lock(self) -> None:
        root = "/var/www/electronic/public_html"

        rebuild = _task_execution_lock_key(root, "campaign_rebuild", 29)
        trigger = _task_execution_lock_key(root, "campaign_trigger", 29)
        other = _task_execution_lock_key(root, "campaign_trigger", 30)

        self.assertEqual(rebuild, trigger)
        self.assertNotEqual(trigger, other)

    def test_audit_rebuild_candidate_spills_ahead_of_regular_ring_when_priority_cap_is_zero(self) -> None:
        priority = deque([40])
        regular = deque([18])

        audit_ids = _merge_campaign_trigger_audit_ids([], [40])

        self.assertEqual(audit_ids, [40])
        self.assertTrue(_campaign_spill_prefers_priority(priority, 0))

        cfg = SimpleNamespace(campaign_rebuild_min_repeat_sec=0, command_timeout_sec=0)
        store = Mock()
        running: dict[str, RunningTask] = {}
        with patch.object(daemon_mod, "_submit_if_slot", return_value=True):
            launched = _fill_from_ring(
                ring=priority,
                ring_limit=1,
                total_limit=1,
                root="/var/www/avio_park/public_html",
                task_type="campaign_rebuild",
                running=running,
                ring_entities={40},
                config=cfg,
                store=store,
                popens={},
                build_args=Mock(return_value=["php", "bin/console", "mautic:campaigns:rebuild", "-i", "40"]),
                remove_on_launch=True,
            )

        self.assertEqual(launched, 1)
        self.assertEqual(list(priority), [])
        self.assertEqual(list(regular), [18])

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

    def test_due_campaign_priority_dispatch_ignores_saturated_segment_lane(self) -> None:
        root = "/var/www/cvetana/public_html"
        running = {
            f"segment-{idx}": SimpleNamespace(
                root=f"/var/www/segment-{idx}",
                task_type="segment",
                entity_id=idx,
            )
            for idx in range(8)
        }
        executor = Mock()
        executor.is_active.return_value = False
        executor.launch.return_value = True
        cfg = SimpleNamespace(
            campaign_trigger_min_repeat_sec=0,
            campaign_trigger_audit_interval_sec=60,
            campaign_trigger_priority_parallel=2,
            php_bin="php",
            mautic_run_as_user="www-data",
            cmd_campaign_trigger_template="mautic:campaigns:trigger -i {id}",
            campaign_limit=0,
            campaign_batch_limit=100,
        )

        with patch.object(daemon_mod, "render_mautic_command", return_value=["php", "bin/console"]):
            launched = _dispatch_due_campaign_triggers(
                config=cfg,
                root=root,
                due_ids=[8, 11],
                running=running,
                priority_executor=executor,
                on_success=Mock(),
            )

        self.assertEqual(launched, [8, 11])
        self.assertEqual(executor.launch.call_count, 2)

    def test_due_campaign_rebuild_liveness_ignores_saturated_segment_lane(self) -> None:
        root = "/var/www/apotekajankovic/public_html"
        running = {
            f"segment-{idx}": SimpleNamespace(
                root=root,
                task_type="segment",
                entity_id=idx,
            )
            for idx in range(8)
        }
        executor = Mock()
        executor.is_active.return_value = False
        executor.launch.return_value = True
        cfg = SimpleNamespace(
            campaign_rebuild_min_repeat_sec=15,
            campaign_rebuild_priority_parallel=3,
            php_bin="php",
            mautic_run_as_user="www-data",
            cmd_campaign_rebuild_template="mautic:campaigns:rebuild -i {id}",
        )

        with patch.object(daemon_mod, "render_mautic_command", return_value=["php", "bin/console"]):
            launched = _dispatch_due_campaign_rebuilds(
                config=cfg,
                root=root,
                due_ids=[222],
                running=running,
                priority_executor=executor,
                on_success=Mock(),
            )

        self.assertEqual(launched, [222])
        kwargs = executor.launch.call_args.kwargs
        self.assertEqual(kwargs["capacity_lane"], "campaign_rebuild_liveness")
        self.assertEqual(kwargs["log_label"], "liveness")

    def test_due_campaign_rebuild_liveness_waits_for_same_campaign_trigger(self) -> None:
        root = "/var/www/site"
        executor = Mock()
        executor.is_active.return_value = False
        cfg = SimpleNamespace(
            campaign_rebuild_min_repeat_sec=15,
            campaign_rebuild_priority_parallel=3,
            php_bin="php",
            mautic_run_as_user="www-data",
            cmd_campaign_rebuild_template="mautic:campaigns:rebuild -i {id}",
        )

        launched = _dispatch_due_campaign_rebuilds(
            config=cfg,
            root=root,
            due_ids=[222],
            running={
                "trigger": SimpleNamespace(root=root, task_type="campaign_trigger", entity_id=222),
            },
            priority_executor=executor,
            on_success=Mock(),
        )

        self.assertEqual(launched, [])
        executor.launch.assert_not_called()

    def test_due_campaign_priority_dispatch_waits_for_same_campaign_rebuild(self) -> None:
        root = "/var/www/cvetana/public_html"
        running = {
            "rebuild-8": SimpleNamespace(root=root, task_type="campaign_rebuild", entity_id=8),
        }
        executor = Mock()
        executor.is_active.return_value = False
        executor.launch.return_value = True
        cfg = SimpleNamespace(
            campaign_trigger_min_repeat_sec=0,
            campaign_trigger_audit_interval_sec=60,
            campaign_trigger_priority_parallel=2,
            php_bin="php",
            mautic_run_as_user="www-data",
            cmd_campaign_trigger_template="mautic:campaigns:trigger -i {id}",
            campaign_limit=0,
            campaign_batch_limit=100,
        )

        with patch.object(daemon_mod, "render_mautic_command", return_value=["php", "bin/console"]):
            launched = _dispatch_due_campaign_triggers(
                config=cfg,
                root=root,
                due_ids=[8, 11],
                running=running,
                priority_executor=executor,
                on_success=Mock(),
            )

        self.assertEqual(launched, [11])
        self.assertEqual(executor.launch.call_args.kwargs["entity_id"], 11)

    def test_due_realtime_campaign_uses_dedicated_trigger_capacity(self) -> None:
        executor = Mock()
        executor.is_active.return_value = False
        executor.launch.return_value = True
        cfg = SimpleNamespace(
            campaign_trigger_min_repeat_sec=30,
            campaign_trigger_audit_interval_sec=60,
            campaign_trigger_priority_parallel=3,
            php_bin="php",
            mautic_run_as_user="www-data",
            cmd_campaign_trigger_template="mautic:campaigns:trigger -i {id}",
            campaign_limit=0,
            campaign_batch_limit=100,
        )
        realtime = CampaignWhitelistRealtimeSetting(
            ids=frozenset({1086, 1087}),
            rebuild_interval_sec=15,
            trigger_interval_sec=15,
            rebuild_parallel=1,
            trigger_parallel=1,
        )

        with patch.object(daemon_mod, "render_mautic_command", return_value=["php", "bin/console"]):
            launched = _dispatch_due_campaign_triggers(
                config=cfg,
                root="/var/www/ss/public_html",
                due_ids=[1086],
                running={},
                priority_executor=executor,
                on_success=Mock(),
                realtime=realtime,
            )

        self.assertEqual(launched, [1086])
        kwargs = executor.launch.call_args.kwargs
        self.assertEqual(kwargs["interval_sec"], 15)
        self.assertEqual(kwargs["max_parallel"], 1)
        self.assertEqual(kwargs["capacity_lane"], "campaign_trigger_realtime")
        self.assertEqual(kwargs["log_label"], "realtime")

    def test_live_campaign_process_scan_finds_untracked_native_child(self) -> None:
        root = "/var/www/mautic"
        process = SimpleNamespace(
            returncode=0,
            stdout=f"42177 php {root}/bin/console mautic:campaigns:trigger -i 589\n",
        )

        with patch.object(daemon_mod.subprocess, "run", return_value=process):
            self.assertTrue(_root_has_live_campaign_process(root, {}))

    def test_live_campaign_process_scan_ignores_another_instance(self) -> None:
        root = "/var/www/mautic"
        process = SimpleNamespace(
            returncode=0,
            stdout="42177 php /var/www/other/bin/console mautic:campaigns:trigger -i 589\n",
        )

        with patch.object(daemon_mod.subprocess, "run", return_value=process):
            self.assertFalse(_root_has_live_campaign_process(root, {}))

    def test_live_campaign_process_scan_fails_closed(self) -> None:
        with patch.object(daemon_mod.subprocess, "run", side_effect=OSError("ps unavailable")):
            self.assertTrue(_root_has_live_campaign_process("/var/www/mautic", {}))

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

    def test_priority_campaign_lanes_share_campaign_total_capacity(self) -> None:
        executor = _PriorityTaskExecutor()
        release = threading.Event()
        started = threading.Event()

        def _run(*_args: object, **_kwargs: object) -> SimpleNamespace:
            started.set()
            release.wait(timeout=2)
            return SimpleNamespace(pid=1234, wait=lambda timeout=None: 0)

        cfg = SimpleNamespace(command_timeout_sec=0, campaign_total_parallel=1)
        with patch.object(daemon_mod, "_spawn_command", side_effect=_run):
            self.assertTrue(
                executor.launch(
                    cfg,
                    root="/var/www/site",
                    task_type="campaign_trigger",
                    entity_id=23,
                    args=["php", "bin/console"],
                    interval_sec=60,
                    max_parallel=1,
                    capacity_lane="campaign_trigger_realtime",
                )
            )
            self.assertTrue(started.wait(timeout=1))
            self.assertFalse(
                executor.launch(
                    cfg,
                    root="/var/www/site",
                    task_type="campaign_rebuild",
                    entity_id=24,
                    args=["php", "bin/console"],
                    interval_sec=60,
                    max_parallel=1,
                    capacity_lane="campaign_rebuild_liveness",
                )
            )
            release.set()
            deadline = time.time() + 2
            while executor.is_active("/var/www/site", "campaign_trigger", 23) and time.time() < deadline:
                time.sleep(0.01)

    def test_realtime_capacity_is_independent_from_normal_priority_capacity(self) -> None:
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
                    entity_id=330,
                    args=["php", "bin/console"],
                    interval_sec=300,
                    max_parallel=1,
                )
            )
            self.assertTrue(started.wait(timeout=1))
            self.assertTrue(
                executor.launch(
                    cfg,
                    root="/var/www/site",
                    task_type="segment",
                    entity_id=332,
                    args=["php", "bin/console"],
                    interval_sec=15,
                    max_parallel=1,
                    capacity_lane="segment_realtime",
                    log_label="realtime",
                )
            )
            release.set()

    def test_realtime_repeat_overrides_normal_segment_cooldown(self) -> None:
        executor = _PriorityTaskExecutor()
        cfg = SimpleNamespace(command_timeout_sec=0, segment_full_scan_interval_sec=300)
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
                    root="/var/www/ss/public_html",
                    task_type="segment",
                    entity_id=332,
                    args=["php", "bin/console"],
                    interval_sec=15,
                    max_parallel=2,
                    capacity_lane="segment_realtime",
                    on_success=completed.set,
                )
            )
            self.assertTrue(completed.wait(timeout=1))

        self.assertTrue(
            daemon_mod._launch_allowed(
                cfg,
                "/var/www/ss/public_html",
                "segment",
                332,
                now_ts=115.0,
                min_repeat_sec=15,
            )
        )
        self.assertFalse(
            daemon_mod._launch_allowed(
                cfg,
                "/var/www/ss/public_html",
                "segment",
                332,
                now_ts=115.0,
            )
        )

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

    def test_whitelists_are_priority_only_without_explicit_realtime(self) -> None:
        cfg = SimpleNamespace(
            disable_whitelist=False,
            segment_whitelist=[],
            segment_whitelist_file=None,
            segment_whitelist_instance_settings={"electronic@host": {"segment_whitelist": [86]}},
            campaign_whitelist=[],
            campaign_whitelist_file=None,
            campaign_whitelist_instance_settings={"electronic@host": {"campaign_whitelist": [61]}},
        )
        inst = SimpleNamespace(
            root="/var/www/electronic/public_html",
            instance_uid="electronic@host",
            primary_domain="electronic.sales-snap.com",
            name="electronic.sales-snap.com",
            domains=[],
        )

        self.assertEqual(_segment_whitelist_effective_setting(cfg, inst), {86})
        self.assertEqual(_campaign_whitelist_effective_setting(cfg, inst), {61})
        self.assertEqual(_priority_realtime_segment_ids(_segment_whitelist_realtime_setting(cfg, inst)), set())
        self.assertEqual(
            _priority_realtime_campaign_rebuild_ids(_campaign_whitelist_realtime_setting(cfg, inst)),
            set(),
        )

    def test_segment_whitelist_orders_due_work_without_injecting_absent_segment(self) -> None:
        priority, regular = _split_segment_circles(
            [90, 91],
            weights={90: 1.0, 91: 2.0},
            whitelist={86},
            threshold=999.0,
            priority_size=0,
        )

        self.assertNotIn(86, priority)
        self.assertNotIn(86, regular)
        self.assertEqual(priority, [])
        self.assertEqual(regular, [91, 90])

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
            patch.object(daemon_mod, "_root_has_live_campaign_process", return_value=False),
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

    def test_campaign_trigger_guard_defers_counter_repair_while_native_trigger_is_live(self) -> None:
        root = "/var/www/enoteka/public_html"
        snapshot = CampaignTriggerProgressSnapshot(
            due_event_logs=0,
            due_root_actions=0,
            due_no_action_branches=0,
            pending_event_logs=0,
            triggered_event_logs=790,
            max_triggered_at="2026-08-14 08:32:27",
            sent_email_stats=679,
            max_email_sent_at="2026-08-14 08:33:16",
        )
        db = Mock()

        with (
            patch.object(daemon_mod.time, "time", return_value=1786696396.0),
            patch.object(daemon_mod, "_campaign_trigger_progress_snapshot", return_value=snapshot),
            patch.object(daemon_mod, "_root_has_live_campaign_process", return_value=True),
            patch.object(daemon_mod, "repair_campaign_email_counters") as repair,
        ):
            skipped = _campaign_trigger_should_skip_launch(
                db=db,
                config=SimpleNamespace(campaign_trigger_due_guard_enabled=True),
                root=root,
                campaign_id=25,
                sql_ctx={},
            )

        self.assertTrue(skipped)
        repair.assert_not_called()
        self.assertNotIn((root, 25), _CAMPAIGN_EMAIL_COUNTER_RECONCILE_AT)
        self.assertEqual(
            _CAMPAIGN_EMAIL_COUNTER_RECONCILE_DEFER_LOG_AT[(root, 25)],
            1786696396.0,
        )

        with (
            patch.object(daemon_mod.time, "time", return_value=1786696456.0),
            patch.object(daemon_mod, "_campaign_trigger_progress_snapshot", return_value=snapshot),
            patch.object(daemon_mod, "_root_has_live_campaign_process", return_value=False),
            patch.object(
                daemon_mod,
                "repair_campaign_email_counters",
                return_value={"checked": 1, "mismatches": 1, "repaired": 1, "skipped": 0},
            ) as repair_after_exit,
        ):
            _campaign_trigger_should_skip_launch(
                db=db,
                config=SimpleNamespace(campaign_trigger_due_guard_enabled=True),
                root=root,
                campaign_id=25,
                sql_ctx={},
            )

        repair_after_exit.assert_called_once_with(db, 25)

    def test_campaign_trigger_guard_counts_prescheduled_rows_as_due(self) -> None:
        due_sql = _campaign_trigger_event_log_due_exists_sql(21)
        progress_sql = _campaign_trigger_event_log_progress_sql(21)
        email_progress_sql = _campaign_trigger_email_progress_sql(21)

        self.assertIn("el.is_scheduled = 1", due_sql)
        self.assertNotIn("el.date_triggered IS NULL", due_sql)
        self.assertNotIn("el.date_triggered < el.trigger_date", due_sql)
        self.assertIn("el.trigger_date <= '{now_event_log}'", due_sql)
        self.assertIn("c.publish_down IS NULL OR c.publish_down >= '{now_utc}'", due_sql)
        self.assertIn("AS next_scheduled_at", progress_sql)
        self.assertIn("el.is_scheduled = 1", progress_sql)
        self.assertIn("SUM(CASE WHEN el.is_scheduled = 1 THEN 1 ELSE 0 END)", progress_sql)
        self.assertIn("pending_event_logs", progress_sql)
        self.assertIn("es.source = 'campaign.event'", email_progress_sql)
        self.assertIn("ce.id = es.source_id", email_progress_sql)
        self.assertIn("ce.campaign_id = 21", email_progress_sql)
        self.assertIn("MAX(es.date_sent)", email_progress_sql)

    def test_campaign_trigger_guard_keeps_merkurosiguranje_future_date_action_until_due(self) -> None:
        root = "/var/www/merkurosiguranje/public_html"
        future = CampaignTriggerProgressSnapshot(
            due_event_logs=0,
            due_root_actions=0,
            due_no_action_branches=0,
            pending_event_logs=468,
            triggered_event_logs=468,
            max_triggered_at="2026-08-07 14:33:37",
            next_scheduled_at="2026-08-10 07:00:00",
        )
        due = CampaignTriggerProgressSnapshot(
            due_event_logs=1,
            due_root_actions=0,
            due_no_action_branches=0,
            pending_event_logs=468,
            triggered_event_logs=468,
            max_triggered_at="2026-08-07 14:33:37",
            next_scheduled_at="2026-08-10 07:00:00",
        )
        sql_ctx = {"now_event_log": "2026-08-10 06:59:46"}

        with (
            patch.object(daemon_mod.time, "time", return_value=1000.0),
            patch.object(daemon_mod, "_campaign_trigger_progress_snapshot", return_value=future) as progress,
        ):
            skipped = _campaign_trigger_should_skip_launch(
                db=Mock(),
                config=SimpleNamespace(campaign_trigger_due_guard_enabled=True),
                root=root,
                campaign_id=56,
                sql_ctx=sql_ctx,
            )

        self.assertTrue(skipped)
        progress.assert_called_once()
        self.assertEqual(_CAMPAIGN_TRIGGER_FUTURE_WAKE[(root, 56)], (1013.0, "2026-08-10 07:00:00"))
        self.assertEqual(_CAMPAIGN_TRIGGER_FUTURE_WAKE_LOGGED[(root, 56)], "2026-08-10 07:00:00")
        self.assertFalse(_campaign_trigger_skip_removes_from_ring(root, 56, set()))

        with (
            patch.object(daemon_mod.time, "time", return_value=1005.0),
            patch.object(daemon_mod, "_campaign_trigger_progress_snapshot") as progress,
        ):
            skipped = _campaign_trigger_should_skip_launch(
                db=Mock(),
                config=SimpleNamespace(campaign_trigger_due_guard_enabled=True),
                root=root,
                campaign_id=56,
                sql_ctx=sql_ctx,
            )

        self.assertTrue(skipped)
        progress.assert_not_called()

        with (
            patch.object(daemon_mod.time, "time", return_value=1013.5),
            patch.object(daemon_mod, "_campaign_trigger_progress_snapshot", return_value=due),
        ):
            skipped = _campaign_trigger_should_skip_launch(
                db=Mock(),
                config=SimpleNamespace(campaign_trigger_due_guard_enabled=True),
                root=root,
                campaign_id=56,
                sql_ctx=sql_ctx,
            )

        self.assertFalse(skipped)
        self.assertNotIn((root, 56), _CAMPAIGN_TRIGGER_FUTURE_WAKE)
        self.assertNotIn((root, 56), _CAMPAIGN_TRIGGER_FUTURE_WAKE_LOGGED)

    def test_campaign_trigger_future_wake_log_is_deduplicated_across_rebuilds(self) -> None:
        root = "/var/www/electronic/public_html"
        future = CampaignTriggerProgressSnapshot(
            due_event_logs=0,
            due_root_actions=0,
            due_no_action_branches=0,
            pending_event_logs=24,
            triggered_event_logs=24,
            max_triggered_at="2026-08-10 06:00:00",
            next_scheduled_at="2026-08-10 08:16:59",
        )
        sql_ctx = {"now_event_log": "2026-08-10 07:32:46"}

        with (
            patch.object(daemon_mod.time, "time", side_effect=(1000.0, 1001.0)),
            patch.object(daemon_mod, "_campaign_trigger_progress_snapshot", return_value=future),
            patch.object(daemon_mod.logging, "info") as info,
        ):
            self.assertTrue(
                _campaign_trigger_should_skip_launch(
                    db=Mock(),
                    config=SimpleNamespace(campaign_trigger_due_guard_enabled=True),
                    root=root,
                    campaign_id=29,
                    sql_ctx=sql_ctx,
                )
            )
            _mark_campaign_rebuild_finished(root, 29, now_ts=1000.5)
            self.assertTrue(
                _campaign_trigger_should_skip_launch(
                    db=Mock(),
                    config=SimpleNamespace(campaign_trigger_due_guard_enabled=True),
                    root=root,
                    campaign_id=29,
                    sql_ctx=sql_ctx,
                )
            )

        self.assertEqual(info.call_count, 1)

    def test_campaign_trigger_ring_preserves_future_wake_but_removes_complete_audit_id(self) -> None:
        root = "/var/www/merkurosiguranje/public_html"
        _CAMPAIGN_TRIGGER_FUTURE_WAKE[(root, 56)] = (1013.0, "2026-08-10 07:00:00")
        ring = deque([56, 19])

        launched = _fill_from_ring(
            ring=ring,
            ring_limit=1,
            total_limit=1,
            root=root,
            task_type="campaign_trigger",
            running={},
            ring_entities={19, 56},
            config=Mock(),
            store=Mock(),
            popens={},
            build_args=Mock(),
            should_skip=lambda _: True,
            remove_on_skip=lambda cid: _campaign_trigger_skip_removes_from_ring(root, cid, set()),
        )

        self.assertEqual(launched, 0)
        self.assertEqual(list(ring), [56])

    def test_campaign_trigger_after_rebuild_preempts_round_robin_rebuild(self) -> None:
        root = "/var/www/frusketerme/public_html"
        trigger_rings = (deque([13, 14]), deque())

        self.assertFalse(_campaign_trigger_after_rebuild_pending(root, trigger_rings))

        _mark_campaign_rebuild_finished(root, 13, now_ts=100.0)

        self.assertTrue(_campaign_trigger_after_rebuild_pending(root, trigger_rings))
        _mark_campaign_trigger_finished(root, 13)
        self.assertFalse(_campaign_trigger_after_rebuild_pending(root, trigger_rings))

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

        _CAMPAIGN_TRIGGER_FUTURE_WAKE[(root, 656)] = (500.0, "2026-08-10 07:00:00")
        _mark_campaign_rebuild_finished(root, 656, now_ts=101.0)

        self.assertFalse(
            _campaign_trigger_waits_for_rebuild(
                root=root,
                campaign_id=656,
                planned_after_ts=100.0,
                running={},
            )
        )
        self.assertNotIn((root, 656), _CAMPAIGN_TRIGGER_FUTURE_WAKE)

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

    def test_terminal_date_ended_overrides_stale_queued_status_for_m6_and_m7(self) -> None:
        for status in (1, "queued", 7, "delayed"):
            self.assertEqual(
                _classify_import_monitor_row(
                    {"status": status, "date_ended": "2026-07-10 09:55:14"}
                ),
                ("done", "", "success"),
            )

    def test_partial_m6_m7_batch_with_terminal_date_stays_live_until_position_finishes(self) -> None:
        partial_rows = (
            {"status": 1, "date_ended": "2026-07-10 09:55:14"},
            {
                "status": 7,
                "date_ended": "2026-07-10 09:55:14",
                "line_count": 13371,
                "properties": '{"line":5101,"defaults":{"list":[25]}}',
            },
            {
                "status": "delayed",
                "date_ended": "2026-07-10 09:55:14",
                "line_count": 13371,
                "properties": {"line": 5101, "defaults": {"list": [26]}},
            },
        )
        self.assertEqual(
            _classify_import_monitor_row(partial_rows[0]),
            ("done", "", "success"),
        )
        self.assertEqual(
            _classify_import_monitor_row(partial_rows[1]),
            ("queued", "delayed", "delayed"),
        )
        self.assertEqual(
            _classify_import_monitor_row(partial_rows[2]),
            ("queued", "delayed", "delayed"),
        )
        terminal = {
            "status": 3,
            "date_ended": "2026-07-10 10:05:14",
            "line_count": 13371,
            "properties": '{"line":13371,"defaults":{"list":[27]}}',
        }
        self.assertEqual(
            _classify_import_monitor_row(terminal),
            ("done", "", "success"),
        )

    def test_import_monitor_partial_row_has_no_completed_followup_until_terminal(self) -> None:
        db = SimpleNamespace(
            fetch_rows=Mock(
                return_value=[
                    {
                        "id": 15,
                        "status": 7,
                        "date_ended": "2026-07-10 09:55:14",
                        "line_count": 13371,
                        "properties": '{"line":5101,"defaults":{"list":[25]}}',
                    },
                    {
                        "id": 16,
                        "status": 3,
                        "date_ended": "2026-07-10 10:05:14",
                        "line_count": 13371,
                        "properties": '{"line":13371,"defaults":{"list":[26]}}',
                    },
                ]
            )
        )
        monitor = _fetch_import_monitor_snapshot(
            db,
            "/var/www/site",
            {
                "now_utc": "2026-07-10 10:10:00",
                "import_pending_cutoff_utc": "2026-07-10 09:00:00",
            },
        )
        self.assertIn("line_count", db.fetch_rows.call_args.args[0])
        self.assertEqual(monitor["queued"], [15])
        self.assertEqual(monitor["done"], [16])
        self.assertEqual(
            _import_monitor_target_segment_ids(monitor, lanes={"done"}, successful_only=True),
            {16: [26]},
        )
        self.assertFalse(_import_completion_requires_full_scan([]))
        self.assertTrue(_import_completion_requires_full_scan({16: [26]}))

    def test_terminal_date_ended_paused_cancelled_and_failed_do_not_hold_target(self) -> None:
        for status, expected in ((5, ("done", "stopped", "stopped")), (4, ("done", "", "error"))):
            self.assertEqual(
                _classify_import_monitor_row(
                    {"status": status, "date_ended": "2026-07-10 09:55:14"}
                ),
                expected,
            )
            monitor = {
                "queued": [], "running": [], "done": [186],
                "item_statuses": {"186": expected[2]},
                "target_segments_by_import": {"186": [63]},
            }
            self.assertEqual(
                _import_monitor_target_segment_ids(monitor, lanes={"queued", "running"}), {}
            )

    def test_active_target_filter_keeps_eligible_and_healthy_imports_fifo(self) -> None:
        monitor = {
            "queued": [101, 102], "running": [103], "done": [104],
            "item_statuses": {"101": "queued", "102": "queued", "103": "processing", "104": "success"},
            "target_segments_by_import": {
                "101": [63], "102": [64], "103": [65], "104": [66],
            },
        }
        self.assertEqual(
            _import_monitor_target_segment_ids(monitor, lanes={"queued", "running"}),
            {101: [63], 102: [64], 103: [65]},
        )

    def test_import_target_segment_uses_explicit_default_list_not_import_id(self) -> None:
        payload = '{"defaults":{"list":"25","tags":["import-15"]}}'

        self.assertEqual(_import_target_segment_ids(payload), [25])
        self.assertEqual(_import_target_segment_ids('{"defaults":{"list":"not-an-id"}}'), [])
        self.assertEqual(_import_target_segment_ids('not-json'), [])

    def test_import_monitor_target_segments_filters_active_and_successful_imports(self) -> None:
        monitor = {
            "queued": [15],
            "running": [16],
            "done": [17, 18],
            "item_statuses": {"15": "queued", "16": "processing", "17": "success", "18": "error"},
            "target_segments_by_import": {"15": [25], "16": [26], "17": [27], "18": [28]},
        }

        self.assertEqual(
            _import_monitor_target_segment_ids(monitor, lanes={"queued", "running"}),
            {15: [25], 16: [26]},
        )
        self.assertEqual(
            _import_monitor_target_segment_ids(monitor, lanes={"done"}, successful_only=True),
            {17: [27]},
        )

    def test_import_monitor_snapshot_reads_target_segment_properties(self) -> None:
        db = SimpleNamespace(
            fetch_rows=Mock(
                return_value=[
                    {
                        "id": 15,
                        "status": 2,
                        "properties": '{"defaults":{"list":25}}',
                    }
                ]
            )
        )

        snapshot = _fetch_import_monitor_snapshot(
            db,
            "/var/www/site",
            {
                "now_utc": "2026-09-01 12:00:00",
                "import_pending_cutoff_utc": "2026-09-01 00:00:00",
            },
        )

        self.assertEqual(snapshot["running"], [15])
        self.assertEqual(snapshot["target_segments_by_import"], {"15": [25]})
        self.assertIn("properties", db.fetch_rows.call_args.args[0])

    def test_orphaned_import_recovery_skips_when_cli_worker_is_alive(self) -> None:
        db = SimpleNamespace(recover_orphaned_imports=Mock(return_value=1))

        with patch("mcd_agent.daemon._root_has_live_import_process", return_value=True):
            recovered = _recover_orphaned_imports_if_safe(db, "/var/www/mautic", {}, grace_sec=60)

        self.assertEqual(recovered, 0)
        db.recover_orphaned_imports.assert_not_called()

    def test_orphaned_import_recovery_accepts_string_root_without_type_error(self) -> None:
        db = SimpleNamespace(recover_orphaned_imports=Mock(return_value=0))
        with patch("mcd_agent.daemon._root_has_live_import_process", return_value=False):
            recovered = _recover_orphaned_imports_if_safe(db, "/var/www/mautic", {})
        self.assertEqual(recovered, 0)
        db.recover_orphaned_imports.assert_called_once()

    def test_import_pending_preserves_configured_queue_when_status_crosscheck_is_empty(self) -> None:
        root = "/var/www/s/public_html"
        db = Mock()
        db.fetch_count.side_effect = [3, 0]
        config = SimpleNamespace(sql_import_pending_count="configured import query")
        sql_ctx = {"import_pending_cutoff_utc": "2026-10-14 04:00:00"}

        with patch.object(daemon_mod.time, "time", return_value=1780000000.0):
            observed = daemon_mod._fetch_import_pending_count(db, config, root, sql_ctx)

        self.assertEqual(observed, 3)
        self.assertEqual(db.fetch_count.call_count, 2)
        self.assertIn(root, daemon_mod._IMPORT_PENDING_OVERRIDE_WARN_TS)

    def test_import_pending_prefers_live_status_crosscheck_when_available(self) -> None:
        db = Mock()
        db.fetch_count.side_effect = [1, 2]
        config = SimpleNamespace(sql_import_pending_count="configured import query")

        self.assertEqual(
            daemon_mod._fetch_import_pending_count(
                db,
                config,
                "/var/www/s/public_html",
                {"import_pending_cutoff_utc": "2026-10-14 04:00:00"},
            ),
            2,
        )

    def test_import_pending_fails_closed_without_durable_baseline(self) -> None:
        db = Mock()
        config = SimpleNamespace(sql_import_pending_count="configured import query")

        self.assertEqual(
            daemon_mod._fetch_import_pending_count(db, config, "/var/www/s/public_html", {}),
            0,
        )
        db.fetch_count.assert_not_called()

    def test_partial_mautic_batch_with_date_ended_keeps_pending_admission(self) -> None:
        root = "/var/www/s/public_html"
        historical_partial_row = {
            "id": 4916,
            "status": 1,
            "date_added": "2026-10-14 03:10:00",
            "date_started": "2026-10-14 03:11:00",
            "date_ended": "2026-10-13 23:59:00",
            "line_count": 13371,
            "properties": '{"line":5101,"defaults":{"list":[25]}}',
        }
        new_partial_row = {
            "id": 4917,
            "status": 1,
            "date_added": "2026-10-14 04:10:00",
            "date_started": "2026-10-14 04:11:00",
            "date_ended": "2026-10-14 04:12:00",
            "line_count": 13371,
            "properties": '{"line":5101,"defaults":{"list":[25]}}',
        }
        terminal_row = {
            "id": 4918,
            "status": 3,
            "date_added": "2026-10-14 04:20:00",
            "date_started": "2026-10-14 04:21:00",
            "date_ended": "2026-10-14 04:22:00",
            "line_count": 13371,
            "properties": '{"line":13371,"defaults":{"list":[26]}}',
        }
        rows = [historical_partial_row, new_partial_row, terminal_row]
        cutoff = "2026-10-14 04:00:00"

        class MauticImportDB:
            def __init__(self) -> None:
                self.count_queries: list[str] = []
                self.count_contexts: list[dict[str, str]] = []

            def fetch_count(self, query: str, *, context: dict[str, str]) -> int:
                self.count_queries.append(query)
                self.count_contexts.append(dict(context))
                launchable = {1, 7, "1", "7", "queued", "pending", "delayed"}
                eligible = [
                    row
                    for row in rows
                    if row["status"] in launchable
                    and row["date_ended"] is None
                    or row["status"] in launchable
                    and str(row["date_ended"]) >= context.get("import_pending_cutoff_utc", "")
                ]
                if "import_pending_cutoff_utc" not in query:
                    return sum(1 for row in eligible if row["date_ended"] is None)
                return len(eligible)

            def fetch_rows(
                self,
                query: str,
                *,
                limit: int,
                context: dict[str, str],
            ) -> list[dict[str, object]]:
                del query, limit, context
                return list(rows)

        db = MauticImportDB()
        config = SimpleNamespace(sql_import_pending_count=_DEFAULT_SQL_IMPORT_PENDING_COUNT)
        sql_ctx = {"now_utc": cutoff, "import_pending_cutoff_utc": cutoff}

        pending = daemon_mod._fetch_import_pending_count(db, config, root, sql_ctx)
        monitor = _fetch_import_monitor_snapshot(
            db,
            root,
            {"now_utc": cutoff, "import_pending_cutoff_utc": cutoff},
        )

        self.assertEqual(pending, 1)
        self.assertIn("import_pending_cutoff_utc", db.count_queries[0])
        self.assertEqual(db.count_contexts[0]["import_pending_cutoff_utc"], cutoff)
        self.assertEqual(monitor["queued"], [4917])
        self.assertEqual(monitor["done"], [4918])
        self.assertEqual(monitor["item_statuses"], {"4917": "queued", "4918": "success"})
        self.assertEqual(
            _import_monitor_target_segment_ids(monitor, lanes={"queued", "running"}),
            {4917: [25]},
        )
        self.assertEqual(
            _import_monitor_target_segment_ids(monitor, lanes={"done"}, successful_only=True),
            {4918: [26]},
        )

        class RuntimeSync:
            def __init__(self) -> None:
                self.values: dict[str, object] = {}

            def get_runtime_sync(self, key: str) -> object:
                return self.values.get(key)

            def put_runtime_sync(self, key: str, value: object) -> None:
                self.values[key] = value

        runtime = RuntimeSync()
        first_cutoff = _ensure_import_pending_cutoff_utc(
            runtime,
            root,
            datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc),
        )
        second_cutoff = _ensure_import_pending_cutoff_utc(
            runtime,
            root,
            datetime(2026, 10, 14, 5, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(first_cutoff, cutoff)
        self.assertEqual(second_cutoff, cutoff)

        with patch.object(daemon_mod, "render_mautic_command", return_value=["fixture", "mautic:import"]), patch.object(
            daemon_mod, "_submit_if_slot", return_value=True
        ) as submit:
            launched = daemon_mod._submit_import_if_segment_slot(
                config=SimpleNamespace(
                    enable_import_polling=True,
                    php_bin="php",
                    mautic_run_as_user="www-data",
                    cmd_import_template="bin/console mautic:import --limit={import_limit}",
                    import_limit=100,
                    command_timeout_sec=60,
                ),
                store=SimpleNamespace(),
                running={},
                popens={},
                root=root,
                cluster_import_allowed=True,
                import_pending_count=pending,
                segment_slot_limit=1,
                now_ts=100.0,
            )

        self.assertTrue(launched)
        self.assertEqual(submit.call_args.kwargs["task_type"], "import")

    def test_import_pending_baseline_persistence_failure_fails_closed(self) -> None:
        class BrokenRuntimeSync:
            def get_runtime_sync(self, key: str) -> object:
                del key
                return None

            def put_runtime_sync(self, key: str, value: object) -> None:
                del key, value
                raise OSError("read-only runtime state")

        self.assertIsNone(
            _ensure_import_pending_cutoff_utc(
                BrokenRuntimeSync(),
                "/var/www/s/public_html",
                datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc),
            )
        )

    def test_import_pending_baseline_readback_mismatch_fails_closed(self) -> None:
        class MismatchedRuntimeSync:
            def get_runtime_sync(self, key: str) -> object:
                del key
                return {"schema": "wrong", "cutoff_utc": "2026-10-14 04:00:00"}

            def put_runtime_sync(self, key: str, value: object) -> None:
                del key, value

        self.assertIsNone(
            _ensure_import_pending_cutoff_utc(
                MismatchedRuntimeSync(),
                "/var/www/s/public_html",
                datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc),
            )
        )

    def test_import_pending_baseline_concurrent_initialization_uses_one_persisted_cutoff(self) -> None:
        class RuntimeSync:
            def __init__(self) -> None:
                self.values: dict[str, object] = {}

            def get_runtime_sync(self, key: str) -> object:
                return self.values.get(key)

            def put_runtime_sync(self, key: str, value: object) -> None:
                self.values[key] = value

        runtime = RuntimeSync()
        results: list[str | None] = []

        def initialize(hour: int) -> None:
            results.append(
                _ensure_import_pending_cutoff_utc(
                    runtime,
                    "/var/www/s/public_html",
                    datetime(2026, 10, 14, hour, 0, tzinfo=timezone.utc),
                )
            )

        workers = [threading.Thread(target=initialize, args=(hour,)) for hour in (4, 5)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(results, ["2026-10-14 04:00:00", "2026-10-14 04:00:00"])

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

    def test_fill_from_ring_logs_import_followup_queue_wait(self) -> None:
        root = "/var/www/site"
        ring = deque([11])
        queued_at = {11: time.time() - 12.0}
        cfg = SimpleNamespace(command_timeout_sec=3600, segment_full_scan_interval_sec=0)

        with patch.object(daemon_mod, "_submit_if_slot", return_value=True), patch.object(
            daemon_mod.logging, "info"
        ) as info:
            launched = _fill_from_ring(
                ring=ring,
                ring_limit=1,
                total_limit=1,
                root=root,
                task_type="segment",
                running={},
                ring_entities={11},
                config=cfg,
                store=SimpleNamespace(),
                popens={},
                build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
                queued_at_by_entity=queued_at,
                remove_on_launch=True,
                max_launches=1,
            )

        self.assertEqual(launched, 1)
        self.assertNotIn(11, queued_at)
        self.assertTrue(
            any(
                call.args[0].endswith("segment scheduler launch id=%s queue_wait_sec=%.3f source=import_followup")
                and call.args[1] == root
                and call.args[2] == 11
                for call in info.call_args_list
            )
        )

    def test_monitor_logs_segment_runtime_on_exit(self) -> None:
        root = "/var/www/site"
        key = _task_key(root, "segment", 11)
        task = RunningTask(
            row_id=7,
            root=root,
            task_key=key,
            task_type="segment",
            entity_id=11,
            command_str="php|bin/console|mautic:segments:update|-i|11",
            timeout_sec=0,
            attempts=1,
            started_at=time.time() - 4.0,
            pid=1234,
        )
        proc = Mock()
        proc.poll.return_value = 0

        with patch.object(daemon_mod, "_mark_segment_finished"), patch.object(
            daemon_mod.logging, "info"
        ) as info:
            _monitor_running(
                config=SimpleNamespace(task_retry_max=1),
                store=Mock(),
                running={key: task},
                popens={key: proc},
            )

        self.assertTrue(
            any(
                call.args[0].endswith("segment scheduler exit id=%s outcome=%s rc=%s runtime_sec=%.3f")
                and call.args[1] == root
                and call.args[2] == 11
                and call.args[3] == "success"
                for call in info.call_args_list
            )
        )

    def test_fill_from_ring_blocks_active_import_target_and_bypasses_only_followup_repeat_guard(self) -> None:
        root = "/var/www/site"
        ring = deque([25, 26])
        cfg = SimpleNamespace(command_timeout_sec=3600, segment_full_scan_interval_sec=300)

        with patch.object(daemon_mod, "_submit_if_slot", return_value=True) as submit:
            launched = _fill_from_ring(
                ring=ring,
                ring_limit=1,
                total_limit=1,
                root=root,
                task_type="segment",
                running={},
                ring_entities={25, 26},
                config=cfg,
                store=SimpleNamespace(),
                popens={},
                build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
                blocked_entities={25},
                bypass_repeat_guard_entities={26},
            )

        self.assertEqual(launched, 1)
        self.assertEqual(submit.call_args.kwargs["entity_id"], 26)
        self.assertTrue(submit.call_args.kwargs["bypass_repeat_guard"])

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

    def test_fill_from_ring_runs_m6_dependency_closure_in_one_worker_slot(self) -> None:
        root = "/var/www/site"
        ring = deque([21, 22, 11, 57, 106, 61, 300])
        context = {
            "schema": "mcd-segment-dispatch-context-v1",
            "mautic_major": 6,
            "parents_by_child": {"11": [21, 22], "57": [11], "106": [11], "61": [57, 106]},
            "children_by_parent": {"21": [11], "22": [11], "11": [57, 106], "57": [61], "106": [61]},
        }

        class Store:
            def __init__(self) -> None:
                self.writes: list[tuple[str, dict[str, object]]] = []

            def get_runtime_sync(self, key: str) -> dict[str, object] | None:
                if key == daemon_mod._segment_dispatch_context_key(root):
                    return context
                return None

            def put_runtime_sync(self, key: str, payload: dict[str, object]) -> None:
                self.writes.append((key, payload))

        store = Store()
        claimed: list[int] = []
        running_ids: list[int] = []
        cfg = SimpleNamespace(command_timeout_sec=3600, segment_full_scan_interval_sec=0)

        with patch.object(daemon_mod, "_submit_if_slot", return_value=True) as submit:
            launched = _fill_from_ring(
                ring=ring,
                ring_limit=3,
                total_limit=3,
                root=root,
                task_type="segment",
                running={},
                ring_entities={21, 22, 11, 57, 106, 61, 300},
                config=cfg,
                store=store,
                popens={},
                build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
                admission_claim=lambda sid: claimed.append(int(sid)) or True,
                admission_running=lambda sid: running_ids.append(int(sid)),
                remove_on_launch=True,
                max_launches=1,
            )

        self.assertEqual(launched, 1)
        submit.assert_called_once()
        self.assertEqual(submit.call_args.kwargs["entity_id"], 21)
        self.assertEqual(claimed, [21, 22, 11, 57, 106, 61])
        self.assertEqual(running_ids, claimed)
        self.assertEqual(submit.call_args.kwargs["args"][:2], ["/bin/sh", "-c"])
        self.assertIn("-i 21", submit.call_args.kwargs["args"][2])
        self.assertIn("-i 61", submit.call_args.kwargs["args"][2])
        self.assertEqual(list(ring), [300])

    def test_fill_from_ring_does_not_pull_remaining_descendant_into_important_chain(self) -> None:
        root = "/var/www/site"
        ring = deque([21, 22, 11, 57, 106, 61])
        context = {
            "schema": "mcd-segment-dispatch-context-v1",
            "mautic_major": 6,
            "parents_by_child": {"11": [21, 22], "57": [11], "106": [11], "61": [57, 106]},
            "children_by_parent": {"21": [11], "22": [11], "11": [57, 106], "57": [61], "106": [61]},
        }
        cycle = {
            "schema": "mcd-segment-cycle-v1",
            "status": "active",
            "items": [21, 22, 11, 57, 106, 61],
            "follow_up": [],
            "important_items": [21, 22, 11, 57, 106],
            "remaining_items": [61],
            "claimed": [],
            "running": [],
            "done": [],
            "errors": [],
        }

        class Store:
            def __init__(self) -> None:
                self.payloads = {
                    daemon_mod._segment_dispatch_context_key(root): context,
                    daemon_mod._segment_cycle_key(root): cycle,
                }

            def get_runtime_sync(self, key: str) -> dict[str, object] | None:
                return self.payloads.get(key)

            def put_runtime_sync(self, key: str, payload: dict[str, object]) -> None:
                self.payloads[key] = payload

        store = Store()
        claimed: list[int] = []
        cfg = SimpleNamespace(command_timeout_sec=3600, segment_full_scan_interval_sec=0)

        with patch.object(daemon_mod, "_submit_if_slot", return_value=True) as submit:
            launched = _fill_from_ring(
                ring=ring,
                ring_limit=1,
                total_limit=1,
                root=root,
                task_type="segment",
                running={},
                ring_entities=set(ring),
                config=cfg,
                store=store,
                popens={},
                build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
                admission_claim=lambda sid: claimed.append(int(sid)) or True,
                remove_on_launch=True,
                bypass_baseline_admission=True,
                max_launches=1,
            )

        self.assertEqual(launched, 1)
        self.assertEqual(claimed, [21, 22, 11, 57, 106])
        self.assertNotIn("-i 61", submit.call_args.kwargs["args"][2])
        self.assertEqual(list(ring), [61])

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

    def test_segment_realtime_subset_is_whitelisted_with_dedicated_limits(self) -> None:
        cfg = SimpleNamespace(
            disable_whitelist=False,
            segment_whitelist=[10],
            segment_whitelist_file=None,
            segment_whitelist_instance_settings={
                "abelapharm.sales-snap.com": {
                    "segment_whitelist": [330],
                    "realtime": {"ids": [332, "333"], "interval_sec": 15, "parallel": 2},
                },
            },
        )
        inst = SimpleNamespace(
            instance_uid="abelapharm.sales-snap.com",
            root="/var/www/ss/public_html",
            name="abelapharm.sales-snap.com",
            primary_domain="abelapharm.sales-snap.com",
            domains=["abelapharm.sales-snap.com"],
        )

        setting = _segment_whitelist_realtime_setting(cfg, inst)

        self.assertEqual(_segment_whitelist_effective_setting(cfg, inst), {330, 332, 333})
        self.assertEqual(setting.ids, frozenset({332, 333}))
        self.assertEqual(setting.interval_sec, 15)
        self.assertEqual(setting.parallel, 2)
        self.assertTrue(setting.enabled)

    def test_campaign_realtime_subset_has_independent_rebuild_and_trigger_slots(self) -> None:
        cfg = SimpleNamespace(
            disable_whitelist=False,
            campaign_whitelist=[],
            campaign_whitelist_file=None,
            campaign_whitelist_instance_settings={
                "/var/www/ss/public_html": {
                    "campaign_whitelist": [],
                    "realtime": {
                        "ids": [1086, 1087],
                        "rebuild_interval_sec": 15,
                        "trigger_interval_sec": 15,
                        "rebuild_parallel": 1,
                        "trigger_parallel": 1,
                    },
                },
            },
        )
        inst = SimpleNamespace(
            instance_uid="abelapharm.sales-snap.com",
            root="/var/www/ss/public_html",
            name="abelapharm.sales-snap.com",
            primary_domain="abelapharm.sales-snap.com",
            domains=["abelapharm.sales-snap.com"],
        )

        setting = _campaign_whitelist_realtime_setting(cfg, inst)

        self.assertEqual(_campaign_whitelist_effective_setting(cfg, inst), {1086, 1087})
        self.assertEqual(setting.ids, frozenset({1086, 1087}))
        self.assertEqual(setting.rebuild_interval_sec, 15)
        self.assertEqual(setting.trigger_interval_sec, 15)
        self.assertEqual(setting.rebuild_parallel, 1)
        self.assertEqual(setting.trigger_parallel, 1)
        self.assertTrue(setting.rebuild_enabled)
        self.assertTrue(setting.trigger_enabled)

    def test_realtime_subset_fails_closed_for_invalid_numeric_limits(self) -> None:
        cfg = SimpleNamespace(
            disable_whitelist=False,
            segment_whitelist=[],
            segment_whitelist_file=None,
            segment_whitelist_instance_settings={
                "abelapharm.sales-snap.com": {
                    "realtime": {
                        "ids": [332, 333],
                        "interval_sec": "immediate",
                        "parallel": {"invalid": True},
                    },
                },
            },
        )
        inst = SimpleNamespace(
            instance_uid="abelapharm.sales-snap.com",
            root="/var/www/ss/public_html",
            name="abelapharm.sales-snap.com",
            primary_domain="abelapharm.sales-snap.com",
            domains=["abelapharm.sales-snap.com"],
        )

        setting = _segment_whitelist_realtime_setting(cfg, inst)

        self.assertEqual(setting.ids, frozenset({332, 333}))
        self.assertEqual(setting.interval_sec, 0)
        self.assertEqual(setting.parallel, 0)
        self.assertFalse(setting.enabled)

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
