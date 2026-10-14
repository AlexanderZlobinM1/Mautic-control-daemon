import sqlite3
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path

from mcd_agent.daemon import (
    RunningTask,
    TaskStore,
    _monitor_cycle_mark_launched,
    _merge_persisted_import_monitor_cycle,
    _merge_persisted_segment_monitor_cycle,
    _publish_scheduler_monitor_cycles,
    _publish_segment_monitor_cycle,
    _scheduler_monitor_plan_key,
)


class SchedulerMonitorCycleTests(unittest.TestCase):
    def _read_payload(self, db_path: Path, root: str) -> dict:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT payload_json FROM runtime_sync WHERE key=?",
            (_scheduler_monitor_plan_key(root),),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        import json

        return json.loads(str(row["payload_json"]))

    def test_segment_cycle_queues_only_not_launched_items_and_resets_after_full_pass(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            store = TaskStore(str(db_path))
            root = "/var/www/mautic"
            cycle_done = {(root, "segment"): {120}}
            running = {
                "segment-51": RunningTask(
                    row_id=1,
                    root=root,
                    task_key=f"{root}|segment|51",
                    task_type="segment",
                    entity_id=51,
                    command_str="php console",
                    timeout_sec=3600,
                    attempts=1,
                    started_at=time.time() - 5,
                    pid=1234,
                )
            }

            _publish_segment_monitor_cycle(
                store=store,
                root=root,
                cycle_done=cycle_done,
                running=running,
                now_ts=1000.0,
                seg_sql_ring=deque(),
                seg_resume_ring=deque(),
                seg_prio_ring=deque(),
                seg_reg_ring=deque([51, 63, 120]),
            )

            payload = self._read_payload(db_path, root)
            cycle = payload["cycles"][0]
            self.assertEqual(cycle["queued"], [63])
            self.assertEqual(cycle["done"], [120])
            self.assertEqual(cycle["running"], [51])

            cycle_done[(root, "segment")] = {51, 63, 120}
            _publish_segment_monitor_cycle(
                store=store,
                root=root,
                cycle_done=cycle_done,
                running={},
                now_ts=1010.0,
                seg_sql_ring=deque(),
                seg_resume_ring=deque(),
                seg_prio_ring=deque(),
                seg_reg_ring=deque([51, 63, 120]),
            )

            payload = self._read_payload(db_path, root)
            cycle = payload["cycles"][0]
            self.assertEqual(cycle["queued"], [51, 63, 120])
            self.assertEqual(cycle["done"], [])
            self.assertEqual(cycle["running"], [])

    def test_durable_pending_cycle_ids_remain_visible_when_volatile_rings_are_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            store = TaskStore(str(db_path))
            root = "/var/www/b2bss"

            # A restart or an empty plan refresh may leave the admitted
            # ordinary cursor only in the durable projection. The monitor
            # must still expose it so Operations can distinguish pending work
            # from a genuinely empty scheduler.
            _publish_scheduler_monitor_cycles(
                store=store,
                root=root,
                cycle_done={(root, "segment"): set()},
                running={},
                now_ts=1000.0,
                seg_sql_ring=deque(),
                seg_resume_ring=deque(),
                seg_prio_ring=deque(),
                seg_reg_ring=deque(),
                campaign_trigger_prio_ring=deque(),
                campaign_trigger_reg_ring=deque(),
                campaign_rebuild_prio_ring=deque(),
                campaign_rebuild_reg_ring=deque(),
                segment_queued_ids=[154, 147, 144, 149, 151],
            )

            payload = self._read_payload(db_path, root)
            cycle = {cycle["task_type"]: cycle for cycle in payload["cycles"]}["segment"]
            self.assertEqual(cycle["queued"], [154, 147, 144, 149, 151])
            self.assertEqual(cycle["done"], [])
            self.assertEqual(cycle["running"], [])
            self.assertEqual(cycle["total"], 5)

    def test_persisted_refill_merges_segment_without_erasing_campaign_or_import_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            store = TaskStore(str(db_path))
            root = "/var/www/parallel-roots"
            _publish_scheduler_monitor_cycles(
                store=store,
                root=root,
                cycle_done={(root, "segment"): set()},
                running={},
                now_ts=1000.0,
                seg_sql_ring=deque(),
                seg_resume_ring=deque(),
                seg_prio_ring=deque(),
                seg_reg_ring=deque(),
                campaign_trigger_prio_ring=deque([501]),
                campaign_trigger_reg_ring=deque(),
                campaign_rebuild_prio_ring=deque(),
                campaign_rebuild_reg_ring=deque(),
                import_monitor={
                    "queued": [701],
                    "running": [],
                    "done": [700],
                    "item_statuses": {"700": "success", "701": "queued"},
                },
            )

            _merge_persisted_segment_monitor_cycle(
                store=store,
                root=root,
                pending_ids=[101, 102],
                done_ids=[100],
                running={
                    "segment-102": RunningTask(
                        row_id=1,
                        root=root,
                        task_key=f"{root}|segment|102",
                        task_type="segment",
                        entity_id=102,
                        command_str="php bin/console mautic:segments:update -i 102",
                        timeout_sec=3600,
                        attempts=1,
                        started_at=999.0,
                        pid=1234,
                    )
                },
                now_ts=1001.0,
            )

            payload = self._read_payload(db_path, root)
            cycles = {cycle["task_type"]: cycle for cycle in payload["cycles"]}
            self.assertEqual(cycles["segment"]["queued"], [101])
            self.assertEqual(cycles["segment"]["running"], [102])
            self.assertEqual(cycles["segment"]["done"], [100])
            self.assertEqual(cycles["campaign_trigger"]["queued"], [501])
            self.assertEqual(cycles["import"]["queued"], [701])
            self.assertEqual(cycles["import"]["done"], [700])

    def test_persisted_refill_replaces_import_cycle_with_fresh_canonical_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            store = TaskStore(str(db_path))
            root = "/var/www/import-refresh"
            _publish_scheduler_monitor_cycles(
                store=store,
                root=root,
                cycle_done={(root, "segment"): set()},
                running={},
                now_ts=1000.0,
                seg_sql_ring=deque(),
                seg_resume_ring=deque(),
                seg_prio_ring=deque(),
                seg_reg_ring=deque(),
                campaign_trigger_prio_ring=deque(),
                campaign_trigger_reg_ring=deque(),
                campaign_rebuild_prio_ring=deque(),
                campaign_rebuild_reg_ring=deque(),
                import_monitor={
                    "queued": [],
                    "running": [],
                    "done": [4916],
                    "item_statuses": {"4916": "success"},
                },
            )

            _merge_persisted_segment_monitor_cycle(
                store=store,
                root=root,
                pending_ids=[101],
                done_ids=[],
                running={},
                now_ts=1001.0,
                import_monitor={
                    "queued": [4917],
                    "running": [],
                    "done": [],
                    "item_statuses": {"4917": "queued"},
                },
            )

            payload = self._read_payload(db_path, root)
            cycles = {cycle["task_type"]: cycle for cycle in payload["cycles"]}
            self.assertEqual(cycles["import"]["queued"], [4917])
            self.assertEqual(cycles["import"]["done"], [])
            self.assertEqual(cycles["import"]["item_statuses"], {"4917": "queued"})

    def test_persisted_refill_replaces_import_cycle_with_fresh_empty_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            store = TaskStore(str(db_path))
            root = "/var/www/import-empty-refresh"
            _publish_scheduler_monitor_cycles(
                store=store,
                root=root,
                cycle_done={(root, "segment"): set()},
                running={},
                now_ts=1000.0,
                seg_sql_ring=deque(),
                seg_resume_ring=deque(),
                seg_prio_ring=deque(),
                seg_reg_ring=deque(),
                campaign_trigger_prio_ring=deque(),
                campaign_trigger_reg_ring=deque(),
                campaign_rebuild_prio_ring=deque(),
                campaign_rebuild_reg_ring=deque(),
                import_monitor={
                    "queued": [],
                    "running": [],
                    "done": [4916],
                    "item_statuses": {"4916": "success"},
                },
            )

            _merge_persisted_segment_monitor_cycle(
                store=store,
                root=root,
                pending_ids=[],
                done_ids=[],
                running={},
                now_ts=1001.0,
                import_monitor={
                    "queued": [],
                    "running": [],
                    "done": [],
                    "item_statuses": {},
                },
            )

            payload = self._read_payload(db_path, root)
            cycles = {cycle["task_type"]: cycle for cycle in payload["cycles"]}
            self.assertEqual(cycles["import"]["queued"], [])
            self.assertEqual(cycles["import"]["running"], [])
            self.assertEqual(cycles["import"]["done"], [])
            self.assertEqual(cycles["import"]["total"], 0)

    def test_idle_import_refresh_preserves_unrelated_cycles_and_advances_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            store = TaskStore(str(db_path))
            root = "/var/www/idle-import-refresh"
            _publish_scheduler_monitor_cycles(
                store=store,
                root=root,
                cycle_done={(root, "segment"): {101}},
                running={},
                now_ts=1000.0,
                seg_sql_ring=deque([102]),
                seg_resume_ring=deque(),
                seg_prio_ring=deque(),
                seg_reg_ring=deque(),
                campaign_trigger_prio_ring=deque([501]),
                campaign_trigger_reg_ring=deque(),
                campaign_rebuild_prio_ring=deque(),
                campaign_rebuild_reg_ring=deque(),
                import_monitor={"queued": [], "running": [], "done": [4916]},
            )
            before = self._read_payload(db_path, root)
            before_cycles = {
                cycle["task_type"]: cycle
                for cycle in before["cycles"]
                if cycle["task_type"] != "import"
            }

            _merge_persisted_import_monitor_cycle(
                store=store,
                root=root,
                import_monitor={"queued": [], "running": [], "done": []},
                now_ts=1001.0,
            )

            after = self._read_payload(db_path, root)
            cycles = {cycle["task_type"]: cycle for cycle in after["cycles"]}
            self.assertEqual(after["updated_at"], 1001.0)
            self.assertEqual(
                {key: cycles[key] for key in before_cycles},
                before_cycles,
            )
            self.assertEqual(cycles["import"]["done"], [])

    def test_idle_import_refresh_ignores_missing_or_invalid_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            store = TaskStore(str(db_path))
            root = "/var/www/idle-import-invalid"
            _publish_scheduler_monitor_cycles(
                store=store,
                root=root,
                cycle_done={},
                running={},
                now_ts=1000.0,
                seg_sql_ring=deque(),
                seg_resume_ring=deque(),
                seg_prio_ring=deque(),
                seg_reg_ring=deque(),
                campaign_trigger_prio_ring=deque(),
                campaign_trigger_reg_ring=deque(),
                campaign_rebuild_prio_ring=deque(),
                campaign_rebuild_reg_ring=deque(),
                import_monitor={"queued": [], "running": [], "done": [4916]},
            )
            before = self._read_payload(db_path, root)

            _merge_persisted_import_monitor_cycle(
                store=store,
                root=root,
                import_monitor=None,
                now_ts=1001.0,
            )
            _merge_persisted_import_monitor_cycle(
                store=store,
                root=root,
                import_monitor={"queued": []},
                now_ts=1002.0,
            )

            self.assertEqual(self._read_payload(db_path, root), before)

    def test_idle_import_refreshes_multiple_roots_independently(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            store = TaskStore(str(db_path))
            roots = ("/var/www/root-a", "/var/www/root-b")
            for root in roots:
                _publish_scheduler_monitor_cycles(
                    store=store,
                    root=root,
                    cycle_done={},
                    running={},
                    now_ts=1000.0,
                    seg_sql_ring=deque(),
                    seg_resume_ring=deque(),
                    seg_prio_ring=deque(),
                    seg_reg_ring=deque(),
                    campaign_trigger_prio_ring=deque(),
                    campaign_trigger_reg_ring=deque(),
                    campaign_rebuild_prio_ring=deque(),
                    campaign_rebuild_reg_ring=deque(),
                    import_monitor={"queued": [], "running": [], "done": [4916]},
                )

            _merge_persisted_import_monitor_cycle(
                store=store,
                root=roots[0],
                import_monitor={"queued": [], "running": [], "done": []},
                now_ts=1001.0,
            )
            _merge_persisted_import_monitor_cycle(
                store=store,
                root=roots[1],
                import_monitor={"queued": [4917], "running": [], "done": []},
                now_ts=1002.0,
            )

            first = {cycle["task_type"]: cycle for cycle in self._read_payload(db_path, roots[0])["cycles"]}
            second = {cycle["task_type"]: cycle for cycle in self._read_payload(db_path, roots[1])["cycles"]}
            self.assertEqual(first["import"]["done"], [])
            self.assertEqual(second["import"]["queued"], [4917])
            self.assertEqual(second["import"]["done"], [])

    def test_campaign_cycles_publish_queued_running_and_done_items(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            store = TaskStore(str(db_path))
            root = "/var/www/mautic"
            cycle_done: dict[tuple[str, str], set[int]] = {}
            running = {
                "trigger-121": RunningTask(
                    row_id=1,
                    root=root,
                    task_key=f"{root}|campaign_trigger|121",
                    task_type="campaign_trigger",
                    entity_id=121,
                    command_str="php console trigger",
                    timeout_sec=3600,
                    attempts=1,
                    started_at=time.time() - 5,
                    pid=2234,
                ),
                "rebuild-122": RunningTask(
                    row_id=2,
                    root=root,
                    task_key=f"{root}|campaign_rebuild|122",
                    task_type="campaign_rebuild",
                    entity_id=122,
                    command_str="php console rebuild",
                    timeout_sec=3600,
                    attempts=1,
                    started_at=time.time() - 5,
                    pid=2235,
                ),
            }
            _monitor_cycle_mark_launched(cycle_done, root=root, task_type="campaign_trigger", entity_id=120)
            _monitor_cycle_mark_launched(cycle_done, root=root, task_type="campaign_rebuild", entity_id=109)

            _publish_scheduler_monitor_cycles(
                store=store,
                root=root,
                cycle_done=cycle_done,
                running=running,
                now_ts=1000.0,
                seg_sql_ring=deque(),
                seg_resume_ring=deque(),
                seg_prio_ring=deque(),
                seg_reg_ring=deque(),
                campaign_trigger_prio_ring=deque([121]),
                campaign_trigger_reg_ring=deque([120, 130]),
                campaign_rebuild_prio_ring=deque([122]),
                campaign_rebuild_reg_ring=deque([109, 114]),
            )

            payload = self._read_payload(db_path, root)
            cycles = {cycle["task_type"]: cycle for cycle in payload["cycles"]}
            self.assertEqual(cycles["campaign_trigger"]["queued"], [130])
            self.assertEqual(cycles["campaign_trigger"]["done"], [120])
            self.assertEqual(cycles["campaign_trigger"]["running"], [121])
            self.assertEqual(cycles["campaign_rebuild"]["queued"], [114])
            self.assertEqual(cycles["campaign_rebuild"]["done"], [109])
            self.assertEqual(cycles["campaign_rebuild"]["running"], [122])

    def test_import_cycle_publishes_mautic_import_ids_and_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            store = TaskStore(str(db_path))
            root = "/var/www/mautic"

            _publish_scheduler_monitor_cycles(
                store=store,
                root=root,
                cycle_done={},
                running={},
                now_ts=1000.0,
                seg_sql_ring=deque(),
                seg_resume_ring=deque(),
                seg_prio_ring=deque(),
                seg_reg_ring=deque(),
                campaign_trigger_prio_ring=deque(),
                campaign_trigger_reg_ring=deque(),
                campaign_rebuild_prio_ring=deque(),
                campaign_rebuild_reg_ring=deque(),
                import_monitor={
                    "queued": [252, 253],
                    "running": [251],
                    "done": [250],
                    "item_variants": {"delayed": [253]},
                    "item_statuses": {"250": "success", "251": "processing", "252": "queued", "253": "delayed"},
                },
            )

            payload = self._read_payload(db_path, root)
            cycle = {cycle["task_type"]: cycle for cycle in payload["cycles"]}["import"]
            self.assertEqual(cycle["queued"], [252, 253])
            self.assertEqual(cycle["running"], [251])
            self.assertEqual(cycle["done"], [250])
            self.assertEqual(cycle["item_variants"], {"delayed": [253]})
            self.assertEqual(
                cycle["item_statuses"],
                {"250": "success", "251": "processing", "252": "queued", "253": "delayed"},
            )

    def test_segment_cycle_marks_sql_ring_items(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            store = TaskStore(str(db_path))
            root = "/var/www/mautic"
            cycle_done: dict[tuple[str, str], set[int]] = {(root, "segment"): {205}}

            _publish_segment_monitor_cycle(
                store=store,
                root=root,
                cycle_done=cycle_done,
                running={},
                now_ts=1000.0,
                seg_sql_ring=deque([204, 205]),
                seg_resume_ring=deque(),
                seg_prio_ring=deque([61]),
                seg_reg_ring=deque(),
            )

            payload = self._read_payload(db_path, root)
            cycle = payload["cycles"][0]
            self.assertEqual(cycle["item_variants"]["sql"], [204, 205])
            self.assertEqual(cycle["queued"], [204, 61])
            self.assertEqual(cycle["done"], [205])

    def test_campaign_cycle_keeps_cooldown_items_out_of_queue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            store = TaskStore(str(db_path))
            root = "/var/www/mautic"
            cycle_done: dict[tuple[str, str], set[int]] = {(root, "campaign_trigger"): {118, 119}}

            _publish_scheduler_monitor_cycles(
                store=store,
                root=root,
                cycle_done=cycle_done,
                running={},
                now_ts=1000.0,
                seg_sql_ring=deque(),
                seg_resume_ring=deque(),
                seg_prio_ring=deque(),
                seg_reg_ring=deque(),
                campaign_trigger_prio_ring=deque([119, 118]),
                campaign_trigger_reg_ring=deque(),
                campaign_rebuild_prio_ring=deque(),
                campaign_rebuild_reg_ring=deque(),
                campaign_trigger_queued_ids=[],
            )

            payload = self._read_payload(db_path, root)
            trigger_cycle = {cycle["task_type"]: cycle for cycle in payload["cycles"]}["campaign_trigger"]
            self.assertEqual(trigger_cycle["queued"], [])
            self.assertEqual(trigger_cycle["done"], [119, 118])

            _publish_scheduler_monitor_cycles(
                store=store,
                root=root,
                cycle_done=cycle_done,
                running={},
                now_ts=1301.0,
                seg_sql_ring=deque(),
                seg_resume_ring=deque(),
                seg_prio_ring=deque(),
                seg_reg_ring=deque(),
                campaign_trigger_prio_ring=deque([119, 118]),
                campaign_trigger_reg_ring=deque(),
                campaign_rebuild_prio_ring=deque(),
                campaign_rebuild_reg_ring=deque(),
                campaign_trigger_queued_ids=[119, 118],
            )

            payload = self._read_payload(db_path, root)
            trigger_cycle = {cycle["task_type"]: cycle for cycle in payload["cycles"]}["campaign_trigger"]
            self.assertEqual(trigger_cycle["queued"], [119, 118])
            self.assertEqual(trigger_cycle["done"], [])


if __name__ == "__main__":
    unittest.main()
