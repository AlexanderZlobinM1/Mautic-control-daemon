from __future__ import annotations

from types import SimpleNamespace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

from mcd_agent import custom_scripts, install_readiness, service_profiles, state_push
from mcd_agent import cli


class AptExternalCallerTests(unittest.TestCase):
    def test_state_push_uses_external_probe_and_collect_when_bound(self) -> None:
        cfg = SimpleNamespace(mcc_push_apt_state_interval_sec=120)
        pusher = state_push.MCCStatePusher(cfg)
        with patch.object(state_push, "_configured_external_operation", side_effect=[
            {"probe_key": "external-probe"},
            {"probe_key": "external-probe"},
            {"status": "ok", "package_count": 7},
        ]) as dispatch:
            self.assertEqual(pusher._apt_probe_key(), "external-probe")
            self.assertEqual(pusher._apt_state(100.0), {"status": "ok", "package_count": 7})
        self.assertEqual(dispatch.call_count, 3)

    def test_state_push_requires_external_probe_binding_without_local_fingerprint(self) -> None:
        cfg = SimpleNamespace(mcc_push_apt_state_interval_sec=120)
        pusher = state_push.MCCStatePusher(cfg)
        with patch.object(state_push, "_configured_external_operation", return_value=None), \
             patch.object(state_push.Path, "stat", side_effect=AssertionError("local APT probe forbidden")):
            with self.assertRaisesRegex(RuntimeError, "apt_state_observe_binding_not_configured"):
                pusher._apt_probe_key()

    def test_install_readiness_uses_external_zabbix_observer_when_bound(self) -> None:
        with patch.object(install_readiness, "_configured_external_operation", return_value={"status": "external"}), \
             patch.object(install_readiness, "reconcile_ipv6_runtime_from_intent"), \
             patch.object(install_readiness, "_cmd_version", return_value=(False, "")), \
             patch.object(install_readiness, "_run", return_value=(1, "")), \
             patch.object(install_readiness, "_php_versions", return_value={}), \
             patch.object(install_readiness, "_docker_state", return_value={}), \
             patch.object(install_readiness, "_database_state", return_value={}), \
             patch.object(install_readiness, "_ipv6_disabled", return_value=False), \
             patch.object(install_readiness, "cloudflare_real_ip_state", return_value={}), \
             patch.object(install_readiness, "installed_runtime_adapters", return_value={}), \
             patch.object(install_readiness, "_path_state", return_value={}):
            result = install_readiness.collect_mautic_install_readiness(SimpleNamespace())
        self.assertEqual(result["zabbix_agent"], {"status": "external"})

    def test_cli_apt_upgrade_replays_events_and_prints_only_payload(self) -> None:
        cfg = SimpleNamespace()
        external = {
            "status": "ok",
            "payload": {
                "status": "ok", "update_rc": 0, "upgrade_rc": 0,
                "duration_sec": 3, "dpkg_conf_policy": "force-confdef + force-confold",
                "postcheck_lines": [],
            },
            "output_events": [{"stream": "stdout", "text": "external-update\n"}],
        }
        with patch.object(sys, "argv", ["mcd-cli", "apt-upgrade", "--config", "/tmp/mcd.toml", "--yes", "--json"]), \
             patch.object(cli, "load_config", return_value=cfg), \
             patch.object(cli, "maybe_notify_update"), \
             patch.object(cli, "_configured_external_operation", return_value=external), \
             patch.object(cli, "_push_state_after_change") as pushed, \
             patch("sys.stdout") as stdout:
            self.assertEqual(cli.main(), 0)
        pushed.assert_called_once_with(cfg, "apt-upgrade")
        rendered = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("external-update", rendered)
        self.assertIn('"update_rc": 0', rendered)

    def test_cli_zabbix_status_wraps_raw_external_marker(self) -> None:
        cfg = SimpleNamespace()
        marker = {"status": "disabled", "reason": "marker_absent"}
        with patch.object(sys, "argv", ["mcd-cli", "zabbix", "status", "--config", "/tmp/mcd.toml"]), \
             patch.object(cli, "load_config", return_value=cfg), \
             patch.object(cli, "maybe_notify_update"), \
             patch.object(cli, "_configured_external_operation", return_value=marker), \
             patch("sys.stdout") as stdout:
            self.assertEqual(cli.main(), 0)
        rendered = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn('"zabbix_mysql_monitor"', rendered)
        self.assertIn('"reason": "marker_absent"', rendered)

    def test_cli_apt_rescan_uses_external_marker_clear(self) -> None:
        cfg = SimpleNamespace()
        cleared = {
            "status": "ok", "removed": True,
            "profiles_count_before": 2, "marker_path": "/run/mcd/apt.marker",
        }
        with patch.object(sys, "argv", [
            "mcd-cli", "service-profile", "rescan", "--component", "apt",
            "--config", "/tmp/mcd.toml", "--dry-run",
        ]), patch.object(cli, "load_config", return_value=cfg), \
             patch.object(cli, "maybe_notify_update"), \
             patch.object(cli, "_configured_external_operation", return_value=cleared) as dispatch, \
             patch.object(cli, "service_profiles_apply_once", return_value={"status": "ok"}), \
             patch("sys.stdout") as stdout:
            self.assertEqual(cli.main(), 0)
        dispatch.assert_called_once_with(
            "apt", {}, cfg=cfg, operation="apt_repo_profile_markers_clear",
            operation_input={"dry_run": True},
        )
        rendered = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn('"profiles_count_before": 2', rendered)

    def test_state_push_accepts_semantic_apt_observation_error(self) -> None:
        cfg = SimpleNamespace(mcc_push_apt_state_interval_sec=120)
        pusher = state_push.MCCStatePusher(cfg)
        observation = {"status": "error", "reason": "apt_cache_unavailable"}
        with patch.object(state_push, "_configured_external_operation", side_effect=[
            {"probe_key": "external-probe"},
            {"probe_key": "external-probe"},
            observation,
        ]):
            self.assertEqual(pusher._apt_probe_key(), "external-probe")
            self.assertEqual(pusher._apt_state(100.0), observation)

    def test_bound_manifest_dispatches_all_apt_receipts(self) -> None:
        handoff = Path(
            "/Users/azlobin/.codex/admin/host-diagnostics/"
            "mcd-twohost-dev-20261101/apt-callsite-replacements-v1.0.0/"
            "final-operation-binding-additions.json"
        )
        if not handoff.is_file():
            self.skipTest("immutable MCC binding handoff is not available in this environment")
        document = json.loads(handoff.read_text(encoding="utf-8"))
        bindings = [row for row in document["operation_bindings"] if row.get("component") == "apt"]
        self.assertEqual(
            {row["operation"] for row in bindings},
            {
                "apt_state_observe",
                "zabbix_state_observe",
                "zabbix_agent_state_observe",
                "apt_repo_profile_markers_clear",
                "apt_upgrade",
            },
        )
        with tempfile.TemporaryDirectory() as cache:
            manifest = Path(cache) / "manifest.json"
            manifest.write_text(json.dumps(document), encoding="utf-8")
            cfg = SimpleNamespace(custom_cache_dir=cache, state_db_path="/var/lib/mcd/state.db")

            def fake_runner(_cfg, **kwargs):
                operation = kwargs["script_key"]
                if operation == "apt-state-observe-v1.0.0":
                    return 0, json.dumps({"probe_key": "opaque"})
                if operation == "zabbix-state-observe-v1.0.0":
                    return 0, json.dumps({"status": "disabled", "reason": "marker_absent"})
                if operation == "zabbix-agent-state-observe-v1.0.0":
                    return 0, json.dumps({"status": "ok", "installed": True})
                if operation == "apt-repo-marker-clear-v1.0.0":
                    return 0, json.dumps({"status": "ok", "removed": True})
                if operation == "apt-cli-upgrade-v1.0.0":
                    return 0, json.dumps({
                        "status": "ok",
                        "payload": {"status": "ok", "update_rc": 0, "upgrade_rc": 0},
                        "output_events": [{"stream": "stdout", "text": "update\n"}],
                    })
                raise AssertionError(operation)

            with patch.object(custom_scripts, "run_custom_script_by_key", side_effect=fake_runner) as runner:
                results = {
                    row["operation"]: service_profiles._configured_external_operation(
                        "apt", {}, cfg=cfg, operation=row["operation"], operation_input={}
                    )
                    for row in bindings
                }

        self.assertEqual(results["apt_state_observe"], {"probe_key": "opaque"})
        self.assertEqual(results["zabbix_state_observe"]["reason"], "marker_absent")
        self.assertEqual(results["zabbix_agent_state_observe"]["installed"], True)
        self.assertEqual(results["apt_repo_profile_markers_clear"]["removed"], True)
        self.assertEqual(results["apt_upgrade"]["payload"]["upgrade_rc"], 0)
        self.assertEqual(runner.call_count, 5)

    def test_mcd_has_no_builtin_apt_profile_closure(self) -> None:
        root = Path(__file__).parents[1]
        self.assertFalse((root / "mcd_agent" / "apt_profile.py").exists())
        for relative in (
            "mcd_agent/cli.py",
            "mcd_agent/install_readiness.py",
            "mcd_agent/service_profiles.py",
            "mcd_agent/state_push.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn("mcd_agent.apt_profile", source)
            self.assertNotIn("apply_apt_profile", source)
            self.assertNotIn("_configured_apt_operation", source)
            self.assertNotIn("_configured_apt_apply", source)
            self.assertNotIn("_configured_zabbix_bootstrap", source)
            for forbidden in (
                "apt-get",
                "apt-cache",
                "apt-mark",
                "add-apt-repository",
                "/etc/apt",
                "/var/lib/apt",
                "/var/lib/dpkg",
            ):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
