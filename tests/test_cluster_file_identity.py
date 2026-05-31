from __future__ import annotations

import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.backup import _cluster_node_slug
from mcd_agent.cluster_routing import cluster_local_identity_values
from mcd_agent.daemon import _cluster_local_full_done_for_date
from mcd_agent.state_push import stable_change_payload, _hash_payload


def _cfg(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "mcc_host_name": None,
        "backup_host_name": None,
        "backup_cluster_files_expected_nodes": [
            "host-37-27-135-183",
            "host-37-27-135-20",
        ],
        "backup_xtrabackup_full_interval_days": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class ClusterFileIdentityTests(unittest.TestCase):
    def test_ip_identity_values_include_mcc_host_slug(self) -> None:
        with patch("mcd_agent.cluster_routing.subprocess.check_output", return_value="37.27.135.183 10.0.0.1\n"):
            vals = cluster_local_identity_values(_cfg())

        self.assertIn("37.27.135.183", vals)
        self.assertIn("host-37-27-135-183", vals)

    def test_cluster_node_slug_prefers_expected_ip_identity(self) -> None:
        with patch("mcd_agent.cluster_routing.subprocess.check_output", return_value="37.27.135.183\n"):
            slug = _cluster_node_slug(_cfg())

        self.assertEqual(slug, "host-37-27-135-183")

    def test_state_change_hash_ignores_volatile_probe_timestamps(self) -> None:
        a = {
            "maintenance_state": {"active": False, "checked_at_utc": "2026-05-11T08:00:00Z"},
            "mautic_install_readiness": {"status": "ok", "checked_at_utc": "2026-05-11T08:00:00Z"},
            "sent_at_utc": "2026-05-11T08:00:01Z",
        }
        b = {
            "maintenance_state": {"active": False, "checked_at_utc": "2026-05-11T08:00:30Z"},
            "mautic_install_readiness": {"status": "ok", "checked_at_utc": "2026-05-11T08:00:30Z"},
            "sent_at_utc": "2026-05-11T08:00:31Z",
        }

        self.assertEqual(_hash_payload(stable_change_payload(a)), _hash_payload(stable_change_payload(b)))

    def test_cluster_full_done_accepts_recent_full_across_utc_date_boundary(self) -> None:
        cfg = _cfg()
        status = {
            "last_local_full_at": "2026-05-10T23:17:18+00:00",
        }

        with patch("mcd_agent.daemon.cluster_backup_status", return_value=status):
            with patch("mcd_agent.daemon.datetime") as dt:
                dt.fromisoformat.side_effect = datetime.fromisoformat
                dt.now.return_value = datetime(2026, 5, 11, 9, 34, 0, tzinfo=timezone.utc)
                self.assertTrue(
                    _cluster_local_full_done_for_date(
                        cfg,
                        datetime(2026, 5, 11, 9, 34, 0, tzinfo=timezone(timedelta(hours=2))),
                    )
                )

    def test_cluster_full_done_rejects_old_full_from_previous_cycle(self) -> None:
        cfg = _cfg()
        status = {
            "last_local_full_at": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
        }

        with patch("mcd_agent.daemon.cluster_backup_status", return_value=status):
            self.assertFalse(_cluster_local_full_done_for_date(cfg, datetime.now()))


if __name__ == "__main__":
    unittest.main()
