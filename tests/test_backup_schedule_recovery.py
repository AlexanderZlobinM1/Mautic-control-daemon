import tempfile
import unittest
from pathlib import Path

from mcd_agent.daemon import TaskStore, _BACKUP_SCHEDULE_STATE_KEY, _backup_schedule_claim, _backup_schedule_receipt


class BackupScheduleRecoveryTests(unittest.TestCase):
    def test_daily_claim_survives_store_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.db")
            first = TaskStore(path)
            self.assertTrue(_backup_schedule_claim(first, "2026-10-22", 100.0, 900.0))
            first.close()
            second = TaskStore(path)
            self.assertFalse(_backup_schedule_claim(second, "2026-10-22", 200.0, 900.0))
            second.close()

    def test_stale_claim_is_recovered(self):
        store = TaskStore(":memory:")
        self.assertTrue(_backup_schedule_claim(store, "2026-10-22", 100.0, 900.0))
        self.assertTrue(_backup_schedule_claim(store, "2026-10-22", 1101.0, 900.0))

    def test_receipt_is_durable(self):
        store = TaskStore(":memory:")
        self.assertTrue(_backup_schedule_claim(store, "2026-10-22", 100.0, 900.0))
        _backup_schedule_receipt(store, "2026-10-22", "failed", 150.0, "probe failed")
        state = store.get_runtime_sync(_BACKUP_SCHEDULE_STATE_KEY)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["claimed_day"], "2026-10-22")


if __name__ == "__main__":
    unittest.main()
