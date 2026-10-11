from __future__ import annotations

import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import daemon


class ImportTerminalMatrixTests(unittest.TestCase):
    """Executable disposable M6/M7 lifecycle matrix; no host or production I/O."""

    def test_m6_m7_terminal_and_live_ownership_matrix(self) -> None:
        terminal_date = "2026-07-10 09:55:14"
        cases = (
            ("m6-stale-queued", {"status": 1, "date_ended": terminal_date}, False),
            ("m7-stale-delayed", {"status": 7, "date_ended": terminal_date}, False),
            ("m6-paused", {"status": 5, "date_ended": terminal_date}, False),
            ("m7-cancelled", {"status": "cancelled", "date_ended": terminal_date}, False),
            ("m6-failed", {"status": 4, "date_ended": terminal_date}, False),
            ("m7-queued", {"status": 1, "date_ended": None}, True),
            ("m6-running", {"status": 2, "date_ended": None}, True),
        )
        receipt = []
        for name, row, owns_target in cases:
            lane, variant, status = daemon._classify_import_monitor_row(row)
            monitor = {
                "queued": [186] if lane == "queued" else [],
                "running": [186] if lane == "running" else [],
                "done": [186] if lane == "done" else [],
                "item_statuses": {"186": status},
                "target_segments_by_import": {"186": [63]},
            }
            targets = daemon._import_monitor_target_segment_ids(
                monitor, lanes={"queued", "running"}
            )
            observed_ownership = bool(targets.get(186) == [63])
            self.assertEqual(observed_ownership, owns_target, name)
            receipt.append({"case": name, "lane": lane, "variant": variant, "owns_target": observed_ownership})
        self.assertEqual(sum(int(item["owns_target"]) for item in receipt), 2)

    def test_fifo_singleton_and_unrelated_segment_progress_on_free_slot(self) -> None:
        monitor = {
            "queued": [101, 102],
            "running": [],
            "done": [],
            "item_statuses": {"101": "queued", "102": "queued"},
            "target_segments_by_import": {"101": [63], "102": [64]},
        }
        targets = daemon._import_monitor_target_segment_ids(monitor, lanes={"queued", "running"})
        self.assertEqual(list(targets), [101, 102])
        self.assertEqual(list(targets.values()), [[63], [64]])

        launched: list[int] = []
        config = SimpleNamespace(command_timeout_sec=60)
        with patch.object(daemon, "_submit_if_slot", return_value=True):
            count = daemon._fill_from_ring(
                ring=deque([70]),
                ring_limit=1,
                total_limit=1,
                root="/sandbox/m6-or-m7",
                task_type="segment",
                running={},
                ring_entities={70},
                config=config,
                store=SimpleNamespace(),
                popens={},
                build_args=lambda sid: ["sandbox-segment-update", str(sid)],
                on_launch=launched.append,
                max_launches=1,
            )
        self.assertEqual(count, 1)
        self.assertEqual(launched, [70])

    def test_retry_and_lost_pid_do_not_reclassify_terminal_import_as_active(self) -> None:
        self.assertEqual(
            daemon._classify_import_monitor_row(
                {"status": 4, "date_ended": "2026-07-10 09:55:14"}
            ),
            ("done", "", "error"),
        )
        retry_monitor = {
            "queued": [201],
            "running": [],
            "done": [],
            "item_statuses": {"201": "queued"},
            "target_segments_by_import": {"201": [63]},
        }
        self.assertEqual(
            daemon._import_monitor_target_segment_ids(retry_monitor, lanes={"queued", "running"}),
            {201: [63]},
        )


if __name__ == "__main__":
    unittest.main()
