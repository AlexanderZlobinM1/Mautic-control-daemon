from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.config import load_config
from mcd_agent.daemon import _campaign_native_fallback_args, _campaign_native_fallback_metrics
from mcd_agent.mode import SUPPORTED_PROFILE_NAMES
from mcd_agent.runtime_overrides import local_runtime_overrides
from mcd_agent.state_push import MCCStatePusher


class CampaignRuntimeConfigTests(unittest.TestCase):
    def test_farm_profile_line_has_distinct_capacity_defaults(self) -> None:
        expected = {
            "farm-tiny": (2, 2, 1, 1),
            "farm-mini": (2, 2, 2, 1),
            "farm-midi": (3, 3, 4, 2),
            "farm-maxi": (7, 4, 8, 4),
            "farm-hiload": (11, 6, 12, 6),
            "farm-ultra": (23, 12, 24, 12),
        }

        self.assertNotIn("farm", SUPPORTED_PROFILE_NAMES)
        for profile, (segment_total, trigger_total, host_limit, instance_limit) in expected.items():
            with self.subTest(profile=profile):
                path = Path(tempfile.mkdtemp()) / "mcd.toml"
                path.write_text(f'[profile]\nname = "{profile}"\n', encoding="utf-8")
                cfg = load_config(str(path), allow_recover_from_mcc=False)
                self.assertIn(profile, SUPPORTED_PROFILE_NAMES)
                self.assertEqual(
                    cfg.segment_priority_parallel_idle + cfg.segment_regular_parallel_idle,
                    segment_total,
                )
                self.assertEqual(
                    cfg.campaign_trigger_priority_parallel + cfg.campaign_trigger_regular_parallel,
                    trigger_total,
                )
                self.assertEqual(cfg.scheduler_host_max_parallel, host_limit)
                self.assertEqual(cfg.scheduler_instance_max_parallel, instance_limit)

    def test_default_campaign_limit_is_unlimited(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text("[runtime]\nprofile_name = \"midi\"\n", encoding="utf-8")

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.campaign_limit, 0)
        self.assertIn("{campaign_limit_arg}", cfg.cmd_campaign_trigger_template)
        self.assertEqual(cfg.campaign_trigger_audit_interval_sec, 60)

    def test_priority_segment_timeout_is_independent_from_unlimited_regular_tasks(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            '[runtime]\nprofile_name = "hiload"\ncommand_timeout_sec = 0\n',
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.command_timeout_sec, 0)
        self.assertEqual(cfg.priority_segment_timeout_sec, 3600)

    def test_active_profile_enforces_one_minute_campaign_audit(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[profile]",
                    'name = "midi"',
                    "[runtime]",
                    "campaign_trigger_audit_interval_sec = 300",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.campaign_trigger_audit_interval_sec, 60)

    def test_passive_profile_keeps_external_campaign_schedule_unchanged(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[profile]",
                    'name = "passive"',
                    "[runtime]",
                    "campaign_trigger_audit_interval_sec = 300",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.campaign_trigger_audit_interval_sec, 300)

    def test_custom_host_scheduler_parallel_limit_is_runtime_configurable(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            '[profile]\nname = "custom"\n[runtime]\nscheduler_host_max_parallel = 6\n',
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.scheduler_host_max_parallel, 6)
        self.assertTrue(cfg.scheduler_elastic_slots_enabled)
        self.assertEqual(cfg.scheduler_emergency_reserved_slots, 1)
        self.assertEqual(cfg.scheduler_instance_max_parallel, 0)
        self.assertEqual(cfg.scheduler_fairness_watchdog_sec, 300)

    def test_custom_profile_runs_without_mcc_and_reports_observed_state_when_available(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[profile]",
                    'name = "custom"',
                    "[runtime]",
                    "scheduler_host_max_parallel = 6",
                    "segment_priority_parallel_idle = 2",
                    "segment_regular_parallel_idle = 3",
                    "segment_retry_count = 3",
                    f'state_db_path = "{path.parent / "state.db"}"',
                    f'scheduler_pause_flag_path = "{path.parent / "scheduler.pause"}"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)
        self.assertEqual(cfg.profile_name, "custom")
        self.assertIsNone(cfg.mcc_url)
        self.assertIsNone(cfg.mcc_token)
        self.assertEqual(cfg.scheduler_host_max_parallel, 6)
        self.assertEqual(cfg.segment_priority_parallel_idle, 2)
        self.assertEqual(cfg.segment_regular_parallel_idle, 3)
        self.assertEqual(cfg.segment_retry_count, 3)
        self.assertFalse(MCCStatePusher(cfg).enabled())

        path.write_text(
            path.read_text(encoding="utf-8") + '\n[mcc]\nurl = "https://mcc.example"\ntoken = "token"\n',
            encoding="utf-8",
        )
        managed_cfg = load_config(str(path), allow_recover_from_mcc=False)
        pusher = MCCStatePusher(managed_cfg)
        self.assertTrue(pusher.enabled())
        payload = {
            "profile": managed_cfg.profile_name,
            "runtime_overrides": local_runtime_overrides(managed_cfg),
        }
        self.assertEqual(payload["profile"], "custom")
        self.assertEqual(payload["runtime_overrides"]["scheduler_host_max_parallel"], 6)
        self.assertEqual(payload["runtime_overrides"]["segment_retry_count"], 3)

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"status":"ok"}'

        with patch("mcd_agent.state_push.request.urlopen", return_value=Response()) as urlopen:
            ok, message = pusher.send(payload)

        self.assertTrue(ok)
        self.assertEqual(message, '{"status":"ok"}')
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://mcc.example/api/v1/agent/state")

    def test_segment_retry_count_is_host_profile_configurable(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            '[profile]\nname = "farm-hiload"\n[runtime]\nsegment_retry_count = 4\n',
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.segment_retry_count, 4)

    def test_segment_retry_count_defaults_to_two_after_profile_application(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text('[profile]\nname = "farm-hiload"\n', encoding="utf-8")

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.segment_retry_count, 2)

    def test_named_profile_capacity_cannot_be_overridden_by_persisted_runtime(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[profile]",
                    'name = "farm-hiload"',
                    "[runtime]",
                    "scheduler_host_max_parallel = 0",
                    "scheduler_instance_max_parallel = 0",
                    "segment_priority_parallel_idle = 0",
                    "segment_regular_parallel_idle = 2",
                    "segment_priority_parallel_throttled = 0",
                    "segment_regular_parallel_throttled = 2",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.profile_name, "farm-hiload")
        self.assertEqual(cfg.scheduler_host_max_parallel, 12)
        self.assertEqual(cfg.scheduler_instance_max_parallel, 6)
        self.assertEqual(cfg.segment_priority_parallel_idle, 8)
        self.assertEqual(cfg.segment_regular_parallel_idle, 3)
        self.assertEqual(cfg.segment_priority_parallel_throttled, 2)
        self.assertEqual(cfg.segment_regular_parallel_throttled, 0)

    def test_page_hits_sql_segments_default_to_quiet_window_only(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text("[runtime]\nprofile_name = \"midi\"\n", encoding="utf-8")

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertTrue(cfg.segment_sql_page_hits_quiet_only)
        self.assertEqual(cfg.segment_sql_page_hits_quiet_hour, 2)
        self.assertEqual(cfg.segment_sql_page_hits_quiet_window_min, 180)
        self.assertEqual(cfg.segment_sql_auto_long_native_min_duration_sec, 600)

    def test_orphan_page_hits_default_to_rolling_24_hour_grace(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text('[runtime]\nprofile_name = "midi"\n', encoding="utf-8")

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.page_hits_orphan_cleanup_grace_min, 1440)

    def test_explicit_orphan_page_hits_grace_is_preserved(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            '[runtime]\nprofile_name = "midi"\npage_hits_orphan_cleanup_grace_min = 90\n',
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.page_hits_orphan_cleanup_grace_min, 90)

    def test_page_hits_sql_quiet_window_can_be_explicitly_disabled(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[runtime]",
                    'profile_name = "midi"',
                    "segment_sql_page_hits_quiet_only = false",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertFalse(cfg.segment_sql_page_hits_quiet_only)

    def test_legacy_campaign_trigger_template_is_migrated(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[runtime]",
                    'profile_name = "midi"',
                    'campaign_limit = "unlimited"',
                    "[commands]",
                    'campaign_trigger_template = "mautic:campaigns:trigger -i {id} --campaign-limit={campaign_limit} --batch-limit={batch_limit}"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.campaign_limit, 0)
        self.assertEqual(
            cfg.cmd_campaign_trigger_template,
            "mautic:campaigns:trigger -i {id}{campaign_limit_arg} --batch-limit={batch_limit}",
        )

    def test_legacy_60000_campaign_limit_is_migrated_to_unlimited(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[runtime]",
                    'profile_name = "midi"',
                    "campaign_limit = 60000",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.campaign_limit, 0)

    def test_active_campaign_trigger_reenables_legacy_disabled_rebuild(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[profile]",
                    'name = "tiny"',
                    "[runtime]",
                    "enable_campaign_rebuild = false",
                    "campaign_trigger_regular_parallel = 1",
                    "campaign_rebuild_regular_parallel = 0",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertTrue(cfg.enable_campaign_rebuild)
        self.assertEqual(cfg.campaign_rebuild_regular_parallel, 1)

    def test_tiny_profile_runs_periodic_segment_full_scan(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[profile]",
                    'name = "tiny"',
                    "[runtime]",
                    "segment_periodic_full_scan_enabled = false",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertTrue(cfg.segment_periodic_full_scan_enabled)
        self.assertEqual(cfg.segment_full_scan_interval_sec, 60)

    def test_farm_maxi_runs_periodic_segment_full_scan_and_keeps_selected_capacity(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(["[profile]", 'name = "farm-maxi"', "",]),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertTrue(cfg.segment_periodic_full_scan_enabled)
        self.assertEqual(cfg.segment_full_scan_interval_sec, 300)
        self.assertEqual(cfg.segment_priority_parallel_idle, 5)
        self.assertEqual(cfg.segment_regular_parallel_idle, 2)
        self.assertEqual(cfg.scheduler_host_max_parallel, 8)
        self.assertEqual(cfg.scheduler_instance_max_parallel, 4)

    def test_every_named_profile_has_literal_scheduler_capacity(self) -> None:
        expected = {
            "passive": (0, 0),
            "tiny": (1, 1),
            "mini": (4, 1),
            "midi": (4, 2),
            "maxi": (8, 4),
            "hiload": (12, 6),
            "ultra": (24, 12),
            "farm-tiny": (1, 1),
            "farm-mini": (2, 1),
            "farm-midi": (4, 2),
            "farm-maxi": (8, 4),
            "farm-hiload": (12, 6),
            "farm-ultra": (24, 12),
        }
        for profile, (host_slots, instance_slots) in expected.items():
            with self.subTest(profile=profile):
                path = Path(tempfile.mkdtemp()) / "mcd.toml"
                path.write_text(
                    "\n".join(["[profile]", f'name = "{profile}"', "",]),
                    encoding="utf-8",
                )

                cfg = load_config(str(path), allow_recover_from_mcc=False)

                self.assertEqual(cfg.scheduler_host_max_parallel, host_slots)
                self.assertEqual(cfg.scheduler_instance_max_parallel, instance_slots)

    def test_selected_high_capacity_profile_is_not_clamped_to_weak_hardware(self) -> None:
        from mcd_agent.hardware_profile import recommended_farm_profile

        self.assertEqual(
            recommended_farm_profile(cpu_count=1, memory_kib=2 * 1024 * 1024),
            "farm-tiny",
        )
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(["[profile]", 'name = "farm-ultra"', "",]),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.profile_name, "farm-ultra")
        self.assertEqual(cfg.scheduler_host_max_parallel, 24)
        self.assertEqual(cfg.scheduler_instance_max_parallel, 12)

    def test_midi_profile_migrates_legacy_single_ring_snapshot(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[profile]",
                    'name = "midi"',
                    "[runtime]",
                    'ring_mode = "single"',
                    "disable_whitelist = true",
                    "segment_priority_size = 0",
                    "segment_priority_parallel_idle = 0",
                    "segment_regular_parallel_idle = 1",
                    "segment_priority_parallel_throttled = 0",
                    "segment_regular_parallel_throttled = 1",
                    "campaign_total_parallel = 1",
                    "campaign_trigger_priority_parallel = 0",
                    "campaign_trigger_regular_parallel = 1",
                    "campaign_rebuild_priority_parallel = 0",
                    "campaign_rebuild_regular_parallel = 1",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.ring_mode, "dual")
        self.assertFalse(cfg.disable_whitelist)
        self.assertEqual(cfg.segment_priority_size, 10)
        self.assertEqual(cfg.segment_priority_parallel_idle, 3)
        self.assertEqual(cfg.segment_regular_parallel_idle, 1)
        self.assertEqual(cfg.campaign_trigger_priority_parallel, 3)
        self.assertEqual(cfg.campaign_rebuild_priority_parallel, 3)

    def test_whitelist_configuration_requires_dual_ring(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[profile]",
                    'name = "farm-hiload"',
                    "[runtime]",
                    'ring_mode = "single"',
                    'segment_whitelist_instance_settings = { "electronic.sales-snap.com" = { "segment_whitelist" = [23, 86] } }',
                    'campaign_whitelist_instance_settings = { "electronic.sales-snap.com" = { "campaign_whitelist" = [29, 61] } }',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.ring_mode, "dual")
        self.assertFalse(cfg.disable_whitelist)

    def test_passive_profile_keeps_campaign_rebuild_disabled(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[profile]",
                    'name = "passive"',
                    "[runtime]",
                    "enable_campaign_rebuild = false",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertFalse(cfg.enable_campaign_rebuild)

    def test_native_campaign_fallback_is_opt_in_and_clamped(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "[runtime]\ncampaign_native_fallback_enabled = true\ncampaign_native_fallback_interval_sec = 1\n",
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertTrue(cfg.campaign_native_fallback_enabled)
        self.assertEqual(cfg.campaign_native_fallback_interval_sec, 300)

    def test_native_campaign_fallback_runs_global_update_before_trigger(self) -> None:
        root = Path(tempfile.mkdtemp())
        console = root / "bin" / "console"
        console.parent.mkdir()
        console.write_text("#!/bin/sh\n", encoding="utf-8")
        cfg = SimpleNamespace(
            php_bin="php",
            mautic_run_as_user=None,
            cmd_campaign_update_template="mautic:campaigns:update -i {id}",
            cmd_campaign_trigger_template="mautic:campaigns:trigger -i {id}{campaign_limit_arg} --batch-limit={batch_limit}",
            campaign_limit=0,
            campaign_batch_limit=500,
        )

        args = _campaign_native_fallback_args(cfg, str(root))

        self.assertEqual(args[:2], ["/bin/sh", "-c"])
        self.assertIn("mautic:campaigns:update --no-interaction; update_rc=$?", args[2])
        self.assertIn("mautic:campaigns:trigger --no-interaction; trigger_rc=$?", args[2])
        self.assertNotIn("--batch-limit", args[2])
        self.assertNotIn(" -i ", args[2])

    def test_native_campaign_fallback_metrics_support_current_mautic_schema(self) -> None:
        class FakeDB:
            def __init__(self) -> None:
                self.count_queries: list[str] = []

            def fetch_rows(self, query: str) -> list[dict[str, str]]:
                self.assert_prefix(query)
                if "campaign_lead_event_log" in query:
                    return [{"Field": name} for name in ("date_triggered", "is_scheduled", "trigger_date")]
                return [{"Field": "source"}]

            def fetch_count(self, query: str) -> int:
                self.assert_prefix(query)
                self.count_queries.append(query)
                return 7 if "campaign_lead_event_log" in query else 11

            @staticmethod
            def assert_prefix(query: str) -> None:
                if "{prefix}" not in query:
                    raise AssertionError(f"missing prefix placeholder: {query}")

        db = FakeDB()
        metrics = _campaign_native_fallback_metrics(db)  # type: ignore[arg-type]

        self.assertEqual(metrics["pending_due"], 7)
        self.assertEqual(metrics["email_stats"], 11)
        self.assertEqual(metrics["error"], "")
        self.assertIn("date_triggered IS NULL", db.count_queries[0])

    def test_native_campaign_fallback_metrics_are_best_effort(self) -> None:
        class PartialDB:
            def fetch_rows(self, query: str) -> list[dict[str, str]]:
                if "campaign_lead_event_log" in query:
                    return [{"Field": "unexpected"}]
                return [{"Field": "source"}]

            @staticmethod
            def fetch_count(query: str) -> int:
                return 13

        metrics = _campaign_native_fallback_metrics(PartialDB())  # type: ignore[arg-type]

        self.assertIsNone(metrics["pending_due"])
        self.assertEqual(metrics["email_stats"], 13)
        self.assertIn("unsupported campaign_lead_event_log schema", str(metrics["error"]))


if __name__ == "__main__":
    unittest.main()
