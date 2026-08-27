from __future__ import annotations

import unittest
from collections import deque

from mcd_agent.daemon import _reconcile_campaign_rings


class CampaignRingReconcileTests(unittest.TestCase):
    def test_fresh_campaign_enters_front_of_rebuild_and_trigger_priority_rings(self) -> None:
        existing = deque([22, 17, 8, 10])

        for _operation in ("rebuild", "trigger"):
            priority, regular = _reconcile_campaign_rings(
                existing.copy(),
                deque([40, 41]),
                [27, 22, 17, 8, 10],
                [40, 41, 42],
            )

            self.assertEqual(list(priority), [27, 22, 17, 8, 10])
            self.assertEqual(list(regular), [40, 41, 42])


if __name__ == "__main__":
    unittest.main()
