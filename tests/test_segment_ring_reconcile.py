from __future__ import annotations

import unittest
from collections import deque

from mcd_agent.ring_utils import reconcile_ring


class SegmentRingReconcileTests(unittest.TestCase):
    def test_default_reconcile_keeps_existing_order_and_appends_new_items(self) -> None:
        ring = reconcile_ring(deque([10, 20, 30]), [20, 30, 40])

        self.assertEqual(list(ring), [20, 30, 40])

    def test_segment_reconcile_can_prioritize_new_items(self) -> None:
        ring = reconcile_ring(deque([10, 20, 30]), [20, 30, 40, 50], new_to_front=True)

        self.assertEqual(list(ring), [40, 50, 20, 30])


if __name__ == "__main__":
    unittest.main()
