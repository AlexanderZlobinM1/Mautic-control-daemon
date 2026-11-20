from __future__ import annotations

import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.backup import _cluster_node_slug
from mcd_agent.cluster_routing import cluster_local_identity_values
from mcd_agent.daemon import _cluster_local_full_done_for_date, _inventory_snapshot_for_push
from mcd_agent.state_push import (
    MCCStatePusher,
    _DEFAULT_STATE_PUSH_TIMEOUT_SEC,
    _hash_payload,
    monitor_signals_change_payload,
    stable_change_payload,
)


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
    def test_state_push_refreshes_inventory_from_shared_state_db(self) -> None:
        current = [SimpleNamespace(root="/var/www/deleted/public_html")]
        fresh = [
            SimpleNamespace(root="/var/www/active/public_html"),
            SimpleNamespace(root="/var/www/retired/public_html"),
        ]

        def active_inventory_must_not_be_used() -> list[SimpleNamespace]:
            raise AssertionError("periodic state payload must use complete inventory")

        inventory = SimpleNamespace(
            list_all_instances=lambda: fresh,
            list_instances=active_inventory_must_not_be_used,
        )

        snapshot, complete = _inventory_snapshot_for_push(inventory, current)

        self.assertIs(snapshot, fresh)
        self.assertTrue(complete)

    def test_periodic_snapshot_keeps_persisted_artifact_after_rescan(self) -> None:
        active = SimpleNamespace(
            instance_uid="active-001",
            root="/var/www/active/public_html",
        )
        artifact = SimpleNamespace(
            instance_uid="gc.sales-snap.com",
            root="/var/www/ss/public_html",
        )
        complete_inventory = [active, artifact]

        class Inventory:
            def list_all_instances(self) -> list[SimpleNamespace]:
                return list(complete_inventory)

            def list_instances(self) -> list[SimpleNamespace]:
                return [active]

        inventory = Inventory()
        current = inventory.list_instances()

        # Model the daemon's repeated autonomous pushes after a rescan.  The
        # active scheduler view remains one row, while each payload view keeps
        # the persisted artifact row available for build_payload().
        first, first_complete = _inventory_snapshot_for_push(inventory, current)
        second, second_complete = _inventory_snapshot_for_push(inventory, current)

        self.assertTrue(first_complete)
        self.assertTrue(second_complete)
        self.assertEqual([row.instance_uid for row in current], ["active-001"])
        self.assertEqual([row.instance_uid for row in first], ["active-001", "gc.sales-snap.com"])
        self.assertEqual([row.instance_uid for row in second], ["active-001", "gc.sales-snap.com"])
        self.assertEqual(second[1].root, "/var/www/ss/public_html")

    def test_state_push_marks_fallback_snapshot_incomplete(self) -> None:
        current = [SimpleNamespace(root="/var/www/active/public_html")]

        class BrokenInventory:
            def list_instances(self):
                raise RuntimeError("state db unavailable")

        snapshot, complete = _inventory_snapshot_for_push(BrokenInventory(), current)

        self.assertIs(snapshot, current)
        self.assertFalse(complete)

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

    def test_monitor_signal_hash_ignores_collection_timestamp(self) -> None:
        a = {
            "monitor_only": True,
            "collected_at_utc": "2026-06-05T20:00:00Z",
            "details": {
                "scheduler": {"planned": [{"root": "/var/www/mautic", "queued": [14]}]},
                "php_console_recent": [],
            },
        }
        b = {
            "monitor_only": True,
            "collected_at_utc": "2026-06-05T20:00:30Z",
            "details": {
                "scheduler": {"planned": [{"root": "/var/www/mautic", "queued": [14]}]},
                "php_console_recent": [],
            },
        }

        self.assertEqual(
            _hash_payload(monitor_signals_change_payload(a)),
            _hash_payload(monitor_signals_change_payload(b)),
        )

    def test_monitor_signal_push_gate_tracks_changes_with_short_throttle(self) -> None:
        pusher = MCCStatePusher(
            SimpleNamespace(
                mcc_push_enabled=True,
                mcc_url="https://mcc.example.test",
                mcc_token="token",
            )
        )
        payload = {
            "details": {
                "scheduler": {"planned": [{"root": "/var/www/mautic", "queued": [14]}]},
                "php_console_recent": [],
            }
        }
        changed_payload = {
            "details": {
                "scheduler": {"planned": [{"root": "/var/www/mautic", "running": [14]}]},
                "php_console_recent": [],
            }
        }

        self.assertTrue(pusher.should_push_monitor_signals(10.0, payload))
        pusher._last_monitor_signals_hash = _hash_payload(monitor_signals_change_payload(payload))
        pusher._last_monitor_signals_push_ts = 10.0
        self.assertFalse(pusher.should_push_monitor_signals(10.1, payload))
        self.assertFalse(pusher.should_push_monitor_signals(10.1, changed_payload))
        self.assertTrue(pusher.should_push_monitor_signals(10.6, changed_payload))

    def test_full_state_push_uses_longer_default_timeout(self) -> None:
        pusher = MCCStatePusher(
            SimpleNamespace(
                mcc_push_enabled=True,
                mcc_url="https://mcc.example.test",
                mcc_token="token",
            )
        )
        calls: dict[str, int] = {}

        class FakeResponse:
            status = 200

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return b'{"status":"ok"}'

        def fake_urlopen(_req, timeout: int):
            calls["timeout"] = timeout
            return FakeResponse()

        with patch("mcd_agent.state_push.request.urlopen", side_effect=fake_urlopen):
            with patch("mcd_agent.state_push.mark_state_snapshot_push_result_mysql"):
                ok, _msg = pusher.send({"schema": "mcd-state-v1"})

        self.assertTrue(ok)
        self.assertEqual(calls["timeout"], _DEFAULT_STATE_PUSH_TIMEOUT_SEC)

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
