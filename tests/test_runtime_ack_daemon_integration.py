from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from mcd_agent import daemon
from mcd_agent.config import load_config
import mcd_agent.runtime_overrides as runtime_overrides


class RuntimeAckDaemonIntegrationTests(TestCase):
    def test_single_cycle_posts_chunks_for_183_key_host_and_normal_instance_ack(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "mcd.toml"
            state_path = root / "state.db"
            config_path.write_text(
                "[profile]\nname = \"custom\"\n"
                "[mcc]\nurl = \"https://mcc.test\"\ntoken = \"token\"\n"
                "[daemon]\npoll_interval_sec = 1\ndispatch_interval_sec = 1\n"
                f"[runtime]\nstate_db_path = \"{state_path}\"\n"
                "mcc_runtime_overrides_poll_enabled = true\n",
                encoding="utf-8",
            )
            config = load_config(str(config_path), allow_recover_from_mcc=False)
            host_runtime = {f"runtime_key_{index:03d}": index for index in range(182)}
            host_runtime["segment_recurring_priority_v1"] = {
                "app.example": {"segments": [7], "max_interval_sec": 60},
            }
            instance_uid = "app.example@MauticFarm-02"
            response = {
                "status": "ok",
                "runtime_overrides": host_runtime,
                "desired_state": {
                    "scope_key": "MauticFarm-02",
                    "revision": 100,
                    "runtime_overrides": host_runtime,
                },
                "instance_desired_states": {
                    instance_uid: {"revision": 7, "runtime_overrides": {"jobs_max_workers": 2}},
                },
            }
            posts: list[dict[str, object]] = []

            class EmptyInventory:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                def count(self) -> int:
                    return 0

                def list_instances(self) -> list[object]:
                    return []

                def rescan(self, *args: object, **kwargs: object) -> int:
                    return 0

            class DisabledPusher:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                def enabled(self) -> bool:
                    return False

            def capture_post(url: str, payload: dict[str, object], token: str | None, timeout_sec: int = 12) -> dict[str, object]:
                posts.append(payload)
                return {"status": "ok"}

            with ExitStack() as stack:
                for target, value in (
                    ("_ensure_mcd_service_kill_mode", False),
                    ("ensure_nginx_4xx_scan_safety", {"status": "ok"}),
                    ("ensure_managed_web_firewall_loopback", {"status": "ok"}),
                    ("fetch_custom_manifest", ([], "fixture")),
                    ("resolve_agent_identity", {"clone_detected": False}),
                    ("_load_orphans_from_store", {}),
                    ("_bootstrap_remote_runtime_before_local_baseline", "none"),
                    ("_initialize_local_runtime_baseline", ({}, "baseline")),
                    ("retire_zabbix_mautic_version_userparameter", {"status": "ok"}),
                    ("_sync_and_publish_form_embed_status", False),
                    ("fetch_runtime_overrides", response),
                    ("_monitor_running", None),
                    ("_refresh_instance_db_maps", None),
                    ("_sync_segment_whitelist_file", None),
                    ("_apply_instance_runtime_guard", None),
                ):
                    stack.enter_context(patch.object(daemon, target, return_value=value))
                stack.enter_context(patch.object(daemon, "InstanceInventory", EmptyInventory))
                stack.enter_context(patch.object(daemon, "MCCStatePusher", DisabledPusher))
                stack.enter_context(patch.object(runtime_overrides, "_post_json", side_effect=capture_post))
                daemon.run_loop(config, single_cycle=True)

            host_posts = [payload["desired_state_ack"]["observed"] for payload in posts if payload["desired_state_ack"]["scope"] == "host"]
            instance_posts = [payload["desired_state_ack"]["observed"] for payload in posts if payload["desired_state_ack"]["scope"] == "instance"]
            self.assertEqual(len(host_posts), 2)
            self.assertEqual(len(instance_posts), 1)
            self.assertTrue(all("runtime_chunk" in observed and "runtime" not in observed for observed in host_posts))
            self.assertEqual(
                set().union(*(set(observed["runtime_chunk"]["runtime"]["keys"]) for observed in host_posts)),
                set(host_runtime),
            )
            self.assertIn("runtime", instance_posts[0])

