from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mcd_agent.config import load_config
from mcd_agent.daemon import _campaign_native_fallback_args, _campaign_native_fallback_metrics


class CampaignRuntimeConfigTests(unittest.TestCase):
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

    def test_host_scheduler_parallel_limit_is_runtime_configurable(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            '[runtime]\nprofile_name = "midi"\nscheduler_host_max_parallel = 6\n',
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.scheduler_host_max_parallel, 6)

    def test_page_hits_sql_segments_default_to_quiet_window_only(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text("[runtime]\nprofile_name = \"midi\"\n", encoding="utf-8")

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertTrue(cfg.segment_sql_page_hits_quiet_only)
        self.assertEqual(cfg.segment_sql_page_hits_quiet_hour, 2)
        self.assertEqual(cfg.segment_sql_page_hits_quiet_window_min, 180)
        self.assertEqual(cfg.segment_sql_auto_long_native_min_duration_sec, 600)

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
