from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.runtime_overrides import (
    acknowledge_desired_runtime_states,
    acknowledge_runtime_state,
    apply_remote_overrides,
    canonical_profile_from_runtime_response,
    fetch_runtime_overrides,
    instance_desired_states,
    local_runtime_overrides,
    merge_instance_desired_states,
    normalize_segment_special_rings,
    overrides_fingerprint,
    push_runtime_overrides,
    runtime_key_observation,
    _runtime_observation_chunks,
    desired_profile_from_runtime_response,
)
from mcd_agent.config import (
    load_config,
    persist_profile_authority,
    reapply_profile_authority,
    remove_runtime_values,
)
from mcd_agent import daemon


class RuntimeOverrideDirectionTests(unittest.TestCase):
    def test_179_key_runtime_observation_is_deterministically_chunked(self) -> None:
        keys = {f"runtime_key_{index}": index for index in range(179)}
        observation = {
            "schema": "mcd-runtime-key-observation-v1",
            "profile": "custom",
            "keys": {
                key: {
                    "status": "unchanged",
                    "desired": value,
                    "effective": value,
                    "source": "mcc",
                    "revision": 94,
                    "reason": "already_effective",
                }
                for key, value in keys.items()
            },
            "effective_runtime": keys,
        }
        batch_id = "a" * 64
        chunks = _runtime_observation_chunks(observation, batch_id=batch_id)
        self.assertGreater(len(chunks), 1)
        self.assertEqual([chunk["part"] for chunk in chunks], list(range(len(chunks))))
        self.assertTrue(all(chunk["total"] == len(chunks) for chunk in chunks))
        self.assertEqual(
            set().union(*(set(chunk["runtime"]["keys"]) for chunk in chunks)),
            set(keys),
        )
        for chunk in chunks:
            encoded = json.dumps({"runtime_chunk": chunk}, separators=(",", ":")).encode()
            self.assertLessEqual(len(chunk["runtime"]["keys"]), 128)
            self.assertLessEqual(len(encoded), 240 * 1024)

    def test_chunk_ack_retries_missing_parts_and_reuses_batch_id_after_restart(self) -> None:
        keys = {f"runtime_key_{index}": index for index in range(179)}
        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token", profile_name="custom")

        class MemoryStore:
            def __init__(self, values=None):
                self.values = dict(values or {})

            def get_runtime_sync(self, key):
                return self.values.get(key)

            def put_runtime_sync(self, key, value):
                self.values[key] = dict(value)

        with patch("mcd_agent.runtime_overrides.runtime_supported_keys", return_value=set(keys)), patch(
            "mcd_agent.runtime_overrides.runtime_remote_allowed_keys", return_value=set(keys)
        ), patch("mcd_agent.runtime_overrides.runtime_effective_map", return_value=keys), patch(
            "mcd_agent.runtime_overrides.acknowledge_runtime_state",
            side_effect=[{"status": "ok"}, {"status": "conflict", "reason": "conflicting-part-fixture"}],
        ) as acknowledge:
            first = MemoryStore()
            failed = acknowledge_desired_runtime_states(
                cfg,
                first,
                desired_state={"scope_key": "MauticFarm-02", "revision": 94},
                host_runtime=keys,
                instance_states={},
                runtime_fingerprint="stable",
            )
        self.assertEqual(len(failed), 1)
        saved = first.values["mcc_runtime_acknowledged"]["host"]
        self.assertEqual(saved["parts"], [0])
        first_call = acknowledge.call_args_list[0]
        first_batch = first_call.kwargs["observed"]["runtime_chunk"]

        resumed_store = MemoryStore(first.values)
        with patch("mcd_agent.runtime_overrides.runtime_supported_keys", return_value=set(keys)), patch(
            "mcd_agent.runtime_overrides.runtime_remote_allowed_keys", return_value=set(keys)
        ), patch("mcd_agent.runtime_overrides.runtime_effective_map", return_value=keys), patch(
            "mcd_agent.runtime_overrides.acknowledge_runtime_state", return_value={"status": "ok"}
        ) as resumed_ack:
            resumed = acknowledge_desired_runtime_states(
                cfg,
                resumed_store,
                desired_state={"scope_key": "MauticFarm-02", "revision": 94},
                host_runtime=keys,
                instance_states={},
                runtime_fingerprint="stable",
            )
        self.assertEqual(resumed, [])
        self.assertTrue(resumed_ack.call_count >= 1)
        self.assertNotEqual(resumed_ack.call_args.kwargs["observed"]["runtime_chunk"]["part"], 0)
        self.assertEqual(resumed_ack.call_args.kwargs["observed"]["runtime_chunk"]["batch_id"], first_batch["batch_id"])

        with patch("mcd_agent.runtime_overrides.runtime_supported_keys", return_value=set(keys)), patch(
            "mcd_agent.runtime_overrides.runtime_remote_allowed_keys", return_value=set(keys)
        ), patch("mcd_agent.runtime_overrides.runtime_effective_map", return_value=keys), patch(
            "mcd_agent.runtime_overrides.acknowledge_runtime_state", return_value={"status": "ok"}
        ) as duplicate_ack:
            self.assertEqual(
                acknowledge_desired_runtime_states(
                    cfg,
                    resumed_store,
                    desired_state={"scope_key": "MauticFarm-02", "revision": 94},
                    host_runtime=keys,
                    instance_states={},
                    runtime_fingerprint="stable",
                ),
                [],
            )
        duplicate_ack.assert_not_called()

    def test_runtime_ack_is_compact_and_contains_no_whole_runtime_dump(self) -> None:
        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
        identity = {
            "effective_hostname": "host-1",
            "effective_mcc_host_name": "MauticFarm-02",
            "local_hostname": "host-1",
            "configured_host_name": "MauticFarm-02",
        }
        observed = runtime_key_observation(
            SimpleNamespace(profile_name="custom", jobs_max_workers=6),
            {"jobs_max_workers": 6},
            revision=92,
            source="mcc",
        )
        with patch(
            "mcd_agent.runtime_overrides.resolve_agent_identity",
            return_value=identity,
        ), patch("mcd_agent.runtime_overrides._post_json", return_value={"status": "ok"}) as post:
            result = acknowledge_runtime_state(
                cfg,
                scope="host",
                scope_key="MauticFarm-02",
                revision=92,
                status="applied",
                observed=observed,
            )

        self.assertEqual(result["status"], "ok")
        payload = post.call_args.args[1]
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        self.assertLess(len(encoded), 64 * 1024)
        ack_observed = payload["desired_state_ack"]["observed"]
        self.assertEqual(set(ack_observed["effective_runtime"]), {"jobs_max_workers"})
        self.assertEqual(set(ack_observed["keys"]), {"jobs_max_workers"})

    def test_runtime_key_observation_reports_profile_lock_and_effective_readback(self) -> None:
        cfg = SimpleNamespace(
            profile_name="farm-hiload",
            scheduler_host_max_parallel=12,
            jobs_max_workers=6,
            config_file_path="",
        )
        observed = runtime_key_observation(
            cfg,
            {"scheduler_host_max_parallel": 6, "jobs_max_workers": 6},
            revision=91,
            source="mcc",
        )
        self.assertEqual(observed["keys"]["scheduler_host_max_parallel"]["status"], "profile_locked")
        self.assertEqual(observed["keys"]["scheduler_host_max_parallel"]["effective"], 12)
        self.assertEqual(observed["keys"]["scheduler_host_max_parallel"]["source"], "profile")
        self.assertEqual(observed["keys"]["jobs_max_workers"]["status"], "unchanged")
        self.assertEqual(observed["keys"]["jobs_max_workers"]["effective"], 6)
        self.assertEqual(observed["keys"]["jobs_max_workers"]["revision"], 91)
        self.assertEqual(observed["effective_runtime"], {
            "scheduler_host_max_parallel": 12,
            "jobs_max_workers": 6,
        })

    def test_empty_instance_ack_is_unverified_and_compact(self) -> None:
        store = SimpleNamespace(values={})
        store.get_runtime_sync = lambda key: store.values.get(key)
        store.put_runtime_sync = lambda key, value: store.values.__setitem__(key, dict(value))
        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
        uid = "empty.example@MauticFarm-02"
        with patch(
            "mcd_agent.runtime_overrides.acknowledge_runtime_state",
            return_value={"status": "ok"},
        ) as acknowledge:
            failures = acknowledge_desired_runtime_states(
                cfg,
                store,
                desired_state=None,
                host_runtime={},
                instance_states={uid: {"revision": 94, "runtime_overrides": {}}},
                runtime_fingerprint="stable",
            )
        self.assertEqual(failures, [])
        self.assertEqual(acknowledge.call_args.kwargs["status"], "unverified")
        self.assertNotIn("runtime", acknowledge.call_args.kwargs["observed"])

    def test_host_and_instance_ack_routes_emit_bounded_requested_key_bytes(self) -> None:
        class MemoryStore:
            def __init__(self):
                self.values = {}

            def get_runtime_sync(self, key):
                return self.values.get(key)

            def put_runtime_sync(self, key, value):
                self.values[key] = dict(value)

        cfg = SimpleNamespace(
            mcc_url="https://mcc.example",
            mcc_token="token",
            profile_name="custom",
            jobs_max_workers=6,
        )
        identity = {
            "effective_hostname": "host-1",
            "effective_mcc_host_name": "MauticFarm-02",
            "local_hostname": "host-1",
            "configured_host_name": "MauticFarm-02",
        }
        uid = "app.example@MauticFarm-02"
        with patch(
            "mcd_agent.runtime_overrides.resolve_agent_identity",
            return_value=identity,
        ), patch("mcd_agent.runtime_overrides._post_json", return_value={"status": "ok"}) as post:
            failures = acknowledge_desired_runtime_states(
                cfg,
                MemoryStore(),
                desired_state={
                    "scope_key": "MauticFarm-02",
                    "revision": 94,
                },
                host_runtime={"jobs_max_workers": 6},
                instance_states={uid: {"revision": 94, "runtime_overrides": {"jobs_max_workers": 6}}},
                runtime_fingerprint="stable",
            )
        self.assertEqual(failures, [])
        self.assertEqual(post.call_count, 2)
        for call in post.call_args_list:
            payload = call.args[1]
            encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
            self.assertLess(len(encoded), 256 * 1024)
            runtime = payload["desired_state_ack"]["observed"]["runtime"]
            self.assertEqual(set(runtime["keys"]), {"jobs_max_workers"})
            self.assertEqual(set(runtime["effective_runtime"]), {"jobs_max_workers"})

    def test_profile_recovery_updates_local_baseline_without_pushing(self) -> None:
        class MemoryStore:
            def __init__(self):
                self.values = {}

            def put_runtime_sync(self, key, value):
                self.values[key] = value

        cfg = SimpleNamespace()
        store = MemoryStore()
        with patch(
            "mcd_agent.daemon.local_runtime_overrides",
            return_value={"segment_recurring_priority_v1": {}},
        ), patch("mcd_agent.daemon.push_runtime_overrides") as push:
            fingerprint = daemon._sync_local_runtime_baseline_after_config_recovery(cfg, store)

        self.assertEqual(
            fingerprint,
            overrides_fingerprint({"segment_recurring_priority_v1": {}}),
        )
        self.assertEqual(store.values["local_runtime"], {"segment_recurring_priority_v1": {}})
        push.assert_not_called()

    def test_restart_without_local_provenance_rebases_runtime_baseline(self) -> None:
        class MemoryStore:
            def __init__(self):
                self.values = {"local_runtime": {"scheduler_host_max_parallel": 4}}

            def get_runtime_sync(self, key):
                return self.values.get(key)

            def put_runtime_sync(self, key, value):
                self.values[key] = value

        cfg = SimpleNamespace(config_file_path="/tmp/mcd-test.toml")
        store = MemoryStore()
        with patch(
            "mcd_agent.daemon.local_runtime_overrides",
            return_value={"scheduler_host_max_parallel": 6},
        ):
            baseline, fingerprint = daemon._initialize_local_runtime_baseline(cfg, store)

        self.assertEqual(baseline, {"scheduler_host_max_parallel": 6})
        self.assertEqual(fingerprint, overrides_fingerprint(baseline))
        self.assertEqual(store.values["local_runtime"], {"scheduler_host_max_parallel": 6})

    def test_restart_profile_snapshot_without_local_provenance_does_not_push(self) -> None:
        class MemoryStore:
            def __init__(self):
                self.values = {
                    "local_runtime": {"jobs_max_workers": 2},
                    "local_runtime_metadata": {
                        "schema": "mcd-runtime-sync-metadata-v1",
                        "clock": 0,
                        "host": {
                            "jobs_max_workers": {
                                "source": "mcc",
                                "event_id": "remote:host:jobs_max_workers",
                                "updated_at": 10,
                                "revision": 91,
                                "tombstone": False,
                            }
                        },
                        "instances": {},
                    },
                }

            def get_runtime_sync(self, key):
                return self.values.get(key)

            def put_runtime_sync(self, key, value):
                self.values[key] = value

        cfg = SimpleNamespace(config_file_path="/tmp/mcd-profile-rebase.toml")
        store = MemoryStore()
        with patch(
            "mcd_agent.daemon.local_runtime_overrides",
            return_value={"jobs_max_workers": 6},
        ):
            baseline, _ = daemon._initialize_local_runtime_baseline(cfg, store)
        self.assertEqual(baseline, {"jobs_max_workers": 6})
        self.assertIsNone(daemon._read_local_runtime_push_pending(store))
        self.assertEqual(store.values["local_runtime"], {"jobs_max_workers": 6})

    def test_poll_rebase_without_provenance_does_not_queue_local_delta(self) -> None:
        class MemoryStore:
            def __init__(self):
                self.values = {"local_runtime": {"jobs_max_workers": 2}}

            def get_runtime_sync(self, key):
                return self.values.get(key)

            def put_runtime_sync(self, key, value):
                self.values[key] = value

        store = MemoryStore()
        pending = daemon._queue_local_runtime_delta(
            store,
            {"jobs_max_workers": 6},
            allow_unproven=False,
        )
        self.assertIsNone(pending)
        self.assertIsNone(daemon._read_local_runtime_push_pending(store))

    def test_local_delta_survives_same_loop_remote_refresh_and_pushes_once(self) -> None:
        class MemoryStore:
            def __init__(self, values=None):
                self.values = dict(values or {})

            def get_runtime_sync(self, key):
                return self.values.get(key)

            def put_runtime_sync(self, key, value):
                self.values[key] = dict(value)

            def delete_runtime_sync(self, keys):
                for key in keys:
                    self.values.pop(key, None)
                return 1

        cfg = SimpleNamespace(config_file_path="/tmp/mcd-race.toml")
        local_edit = {
            "segment_super_priority_instance_settings": {
                "generic.sales-snap.com": {"segments": [201, 202], "slots_override": 1},
            },
        }
        store = MemoryStore({"local_runtime": {}})

        # The edit is durably captured before the simulated remote poll is
        # allowed to refresh any runtime baseline.
        pending = daemon._queue_local_runtime_delta(store, local_edit)
        self.assertEqual(pending, {"previous": {}, "current": local_edit})

        # Model the race: remote apply attempts to replace the local baseline.
        store.put_runtime_sync("local_runtime", {"scheduler_host_max_parallel": 9})
        with patch("mcd_agent.daemon.local_runtime_overrides", return_value=local_edit):
            restarted_baseline, _fingerprint = daemon._initialize_local_runtime_baseline(cfg, store)
        self.assertEqual(restarted_baseline, {})
        self.assertEqual(store.values["local_runtime"], {})
        self.assertEqual(
            store.values[daemon._LOCAL_RUNTIME_PUSH_PENDING_KEY],
            {"previous": {}, "current": local_edit},
        )

        install = SimpleNamespace(
            instance_uid="generic.sales-snap.com",
            root="/var/www/generic/public_html",
            name="generic",
            primary_domain="generic.sales-snap.com",
            domains=["generic.sales-snap.com"],
        )
        qualified_uid = "generic.sales-snap.com@MauticFarm-02"
        fresh = {
            "status": "ok",
            "runtime_overrides": {},
            "instance_desired_states": {
                qualified_uid: {
                    "revision": 6,
                    "runtime_overrides": {
                        "segment_whitelist_instance_settings": {"segments": [909]},
                    },
                },
            },
            "desired_state": {"revision": 10},
        }
        accepted = {
            "status": "ok",
            "desired_state": {"revision": 11},
            "instance_desired_states": {qualified_uid: {"revision": 7}},
        }
        with patch("mcd_agent.daemon.fetch_runtime_overrides", return_value=fresh), patch(
            "mcd_agent.daemon.push_runtime_overrides", return_value=accepted
        ) as push:
            result = daemon._push_local_runtime_change(
                SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token"),
                pending["previous"],
                pending["current"],
                [install],
                desired_revision=9,
                instance_revisions={qualified_uid: 5},
            )

        self.assertEqual(result, accepted)
        self.assertEqual(push.call_count, 1)
        row = push.call_args.kwargs["instance_desired_states"][qualified_uid]
        self.assertEqual(row["segment_whitelist_instance_settings"], {"segments": [909]})
        self.assertEqual(
            row["segment_super_priority_instance_settings"],
            {"segments": [201, 202], "slots_override": 1},
        )

        self.assertTrue(daemon._store_desired_runtime_push_result(store, result))
        store.put_runtime_sync("local_runtime", local_edit)
        daemon._clear_local_runtime_push_pending(store)
        self.assertIsNone(daemon._read_local_runtime_push_pending(store))
        # The accepted revision is the only state transition that clears the
        # durable delta; a second pass has nothing left to push.
        self.assertIsNone(daemon._queue_local_runtime_delta(store, local_edit))
        self.assertEqual(store.values["mcc_runtime_desired_state"]["revision"], 11)

    def test_pending_local_delta_is_not_cleared_by_incomplete_push_ack(self) -> None:
        class MemoryStore:
            def __init__(self):
                self.values = {
                    "local_runtime": {},
                    daemon._LOCAL_RUNTIME_PUSH_PENDING_KEY: {
                        "previous": {},
                        "current": {"scheduler_host_max_parallel": 6},
                    },
                }

            def get_runtime_sync(self, key):
                return self.values.get(key)

            def put_runtime_sync(self, key, value):
                self.values[key] = dict(value)

            def delete_runtime_sync(self, keys):
                for key in keys:
                    self.values.pop(key, None)
                return 1

        store = MemoryStore()
        self.assertFalse(daemon._store_desired_runtime_push_result(store, {"status": "ok"}))
        self.assertIsNotNone(daemon._read_local_runtime_push_pending(store))
        self.assertFalse(
            daemon._store_desired_runtime_push_result(
                store,
                {"status": "ok", "desired_state": {"revision": "11"}},
            )
        )
        self.assertIsNotNone(daemon._read_local_runtime_push_pending(store))

    def test_poll_requests_local_and_host_qualified_instance_uid_aliases(self) -> None:
        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
        identity = {
            "effective_hostname": "MauticFarm-02",
            "effective_mcc_host_name": "MauticFarm-02",
            "local_hostname": "MauticFarm-02",
            "configured_host_name": "",
        }
        with patch("mcd_agent.runtime_overrides.resolve_agent_identity", return_value=identity), patch(
            "mcd_agent.runtime_overrides._post_json", return_value={"status": "ok"}
        ) as post:
            fetch_runtime_overrides(cfg, instance_uids=["electronic.sales-snap.com"])

        payload = post.call_args.args[1]
        self.assertEqual(
            payload["instance_uids"],
            ["electronic.sales-snap.com", "electronic.sales-snap.com@MauticFarm-02"],
        )

    def test_local_runtime_push_targets_canonical_desired_state_when_requested(self) -> None:
        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
        identity = {
            "effective_hostname": "host-1",
            "effective_mcc_host_name": "host-1",
            "local_hostname": "host-1",
            "configured_host_name": "",
        }
        with patch("mcd_agent.runtime_overrides.resolve_agent_identity", return_value=identity), patch(
            "mcd_agent.runtime_overrides._post_json", return_value={"status": "ok"}
        ) as post:
            result = push_runtime_overrides(
                cfg,
                {
                    "scheduler_host_max_parallel": 6,
                    "segment_retry_count": 3,
                },
                target="desired",
                desired_state_revision=11,
                instance_desired_states={
                    "generic.sales-snap.com@MauticFarm-02": {
                        "segment_super_priority_instance_settings": {
                            "segments": [101, 102],
                            "slots_override": 1,
                        }
                    }
                },
                instance_desired_state_revisions={
                    "generic.sales-snap.com@MauticFarm-02": 5,
                },
            )

        self.assertEqual(result["status"], "ok")
        payload = post.call_args.args[1]
        self.assertEqual(payload["target"], "desired")
        self.assertEqual(payload["push_mode"], "replace")
        self.assertEqual(payload["desired_state_protocol"], 1)
        self.assertEqual(payload["desired_state_revision"], 11)
        self.assertEqual(payload["runtime_overrides"]["segment_retry_count"], 3)
        self.assertEqual(
            payload["instance_desired_state_revisions"]["generic.sales-snap.com@MauticFarm-02"],
            5,
        )

    def test_local_runtime_snapshot_excludes_daemon_bootstrap_keys(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "mcd.toml"
            path.write_text(
                '[profile]\nname = "custom"\n\n'
                '[runtime]\n'
                'dispatch_interval_sec = 0.2\n'
                'poll_interval_sec = 1\n'
                'php_bin = "/usr/bin/php"\n'
                'import_stall_timeout_sec = 900\n'
                'segment_retry_count = 2\n',
                encoding="utf-8",
            )

            cfg = load_config(str(path), allow_recover_from_mcc=False)
            runtime = local_runtime_overrides(cfg)

        self.assertEqual(runtime["import_stall_timeout_sec"], 900)
        self.assertEqual(runtime["segment_retry_count"], 2)
        self.assertNotIn("dispatch_interval_sec", runtime)
        self.assertNotIn("poll_interval_sec", runtime)
        self.assertNotIn("php_bin", runtime)

    def test_desired_push_projects_host_and_instance_payloads_through_contract(self) -> None:
        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
        identity = {
            "effective_hostname": "host-1",
            "effective_mcc_host_name": "host-1",
            "local_hostname": "host-1",
            "configured_host_name": "",
        }
        response = {
            "status": "ok",
            "desired_state": {"revision": 4, "runtime_overrides": {"import_stall_timeout_sec": 900}},
            "instance_desired_states": {"example@host-1": {"revision": 2, "runtime_overrides": {}}},
        }
        with patch("mcd_agent.runtime_overrides.resolve_agent_identity", return_value=identity), patch(
            "mcd_agent.runtime_overrides._post_json", return_value=response
        ) as post:
            result = push_runtime_overrides(
                cfg,
                {
                    "import_stall_timeout_sec": 900,
                    "dispatch_interval_sec": 0.2,
                    "poll_interval_sec": 1,
                    "future_local_only_key": "keep-local",
                },
                target="desired",
                desired_state_revision=3,
                instance_desired_states={
                    "example@host-1": {
                        "import_stall_timeout_sec": 900,
                        "dispatch_interval_sec": 0.2,
                        "future_local_only_key": "keep-local",
                    },
                },
            )

        self.assertEqual(result["status"], "ok")
        payload = post.call_args.args[1]
        self.assertEqual(payload["runtime_overrides"], {"import_stall_timeout_sec": 900})
        self.assertEqual(
            payload["instance_desired_states"],
            {"example@host-1": {"import_stall_timeout_sec": 900}},
        )

    def test_conflicting_local_change_rebases_latest_mcc_state_before_retry(self) -> None:
        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
        install = SimpleNamespace(
            instance_uid="generic.sales-snap.com",
            root="/var/www/generic/public_html",
            name="generic",
            primary_domain="generic.sales-snap.com",
            domains=["generic.sales-snap.com"],
        )
        previous = {
            "scheduler_host_max_parallel": 4,
            "segment_priority_instance_settings": {
                "generic.sales-snap.com": {"segments": [101]},
            },
        }
        current = {
            "scheduler_host_max_parallel": 6,
            "segment_priority_instance_settings": {
                "generic.sales-snap.com": {"segments": [102]},
            },
        }
        fresh = {
            "status": "ok",
            "runtime_overrides": {
                "scheduler_host_max_parallel": 5,
                "scheduler_instance_max_parallel": 7,
                "segment_priority_instance_settings": {
                    "generic.sales-snap.com@MauticFarm-02": {"segments": [999]},
                    "other.sales-snap.com@MauticFarm-02": {"segments": [303]},
                },
            },
            "instance_desired_states": {
                "generic.sales-snap.com@MauticFarm-02": {
                    "runtime_overrides": {
                        "segment_priority_instance_settings": {"segments": [999]},
                    },
                    "revision": 8,
                },
                "other.sales-snap.com@MauticFarm-02": {
                    "runtime_overrides": {
                        "segment_priority_instance_settings": {"segments": [303]},
                    },
                    "revision": 4,
                },
            },
            "desired_state": {"revision": 12},
        }
        pushed_ok = {
            "status": "ok",
            "desired_state": {"revision": 13},
            "instance_desired_states": {
                "generic.sales-snap.com@MauticFarm-02": {"revision": 9},
            },
        }
        with patch("mcd_agent.daemon.fetch_runtime_overrides", return_value=fresh) as fetch, patch(
            "mcd_agent.daemon.push_runtime_overrides",
            side_effect=[{"status": "conflict"}, pushed_ok],
        ) as push:
            result = daemon._push_local_runtime_change(
                cfg,
                previous,
                current,
                [install],
                desired_revision=11,
                instance_revisions={"generic.sales-snap.com@MauticFarm-02": 7},
            )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["_local_rebase_attempted"])
        self.assertEqual(fetch.call_count, 2)
        fetch.assert_any_call(cfg, instance_uids=["generic.sales-snap.com"])
        self.assertEqual(push.call_count, 2)
        self.assertEqual(
            push.call_args_list[0].kwargs["instance_desired_states"],
            {
                "generic.sales-snap.com@MauticFarm-02": {
                    "segment_priority_instance_settings": {"segments": [102]},
                },
                "other.sales-snap.com@MauticFarm-02": {
                    "segment_priority_instance_settings": {"segments": [303]},
                },
            },
        )
        self.assertEqual(
            push.call_args_list[0].kwargs["instance_desired_state_revisions"],
            {
                "generic.sales-snap.com@MauticFarm-02": 8,
                "other.sales-snap.com@MauticFarm-02": 4,
            },
        )
        payload = push.call_args.args[1]
        self.assertEqual(payload["scheduler_host_max_parallel"], 6)
        self.assertEqual(payload["scheduler_instance_max_parallel"], 7)
        self.assertEqual(
            payload["segment_priority_instance_settings"]["generic.sales-snap.com@MauticFarm-02"],
            {"segments": [102]},
        )
        self.assertEqual(
            payload["segment_priority_instance_settings"]["other.sales-snap.com@MauticFarm-02"],
            {"segments": [303]},
        )
        self.assertEqual(push.call_args.kwargs["desired_state_revision"], 12)
        self.assertEqual(
            push.call_args.kwargs["instance_desired_states"],
            {
                "generic.sales-snap.com@MauticFarm-02": {
                    "segment_priority_instance_settings": {"segments": [102]},
                },
                "other.sales-snap.com@MauticFarm-02": {
                    "segment_priority_instance_settings": {"segments": [303]},
                },
            },
        )
        self.assertEqual(
            push.call_args.kwargs["instance_desired_state_revisions"],
            {
                "generic.sales-snap.com@MauticFarm-02": 8,
                "other.sales-snap.com@MauticFarm-02": 4,
            },
        )
        class MemoryStore:
            def __init__(self):
                self.values = {}

            def put_runtime_sync(self, key, value):
                self.values[key] = value

        store = MemoryStore()
        self.assertTrue(daemon._store_desired_runtime_push_result(store, result))
        self.assertEqual(
            store.values["mcc_runtime_desired_state"],
            {"revision": 13, "instance_revisions": {"generic.sales-snap.com@MauticFarm-02": 9}},
        )

    def test_local_restart_preserves_remote_special_ring_when_baseline_has_no_instance_key(self) -> None:
        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
        install = SimpleNamespace(
            instance_uid="generic.sales-snap.com",
            root="/var/www/generic/public_html",
            name="generic",
            primary_domain="generic.sales-snap.com",
            domains=["generic.sales-snap.com"],
        )
        previous = {"scheduler_host_max_parallel": 4}
        current = {"scheduler_host_max_parallel": 6}
        qualified_uid = "generic.sales-snap.com@MauticFarm-02"
        fresh = {
            "status": "ok",
            "runtime_overrides": {"scheduler_host_max_parallel": 5},
            "instance_desired_states": {
                qualified_uid: {
                    "revision": 5,
                    "runtime_overrides": {
                        "segment_special_rings_v1": {
                            "priority": [],
                            "super_priority": {"segments": [101, 102], "slots_override": 1},
                        },
                    },
                },
            },
            "desired_state": {"revision": 7},
        }
        with patch("mcd_agent.daemon.fetch_runtime_overrides", return_value=fresh), patch(
            "mcd_agent.daemon.push_runtime_overrides",
            return_value={"status": "ok", "desired_state": {"revision": 8}},
        ) as push:
            result = daemon._push_local_runtime_change(
                cfg,
                previous,
                current,
                [install],
                desired_revision=6,
                instance_revisions={qualified_uid: 4},
            )

        self.assertEqual(result["status"], "ok")
        payload = push.call_args.args[1]
        self.assertEqual(
            payload["segment_super_priority_instance_settings"][qualified_uid],
            {"segments": [101, 102], "slots_override": 1},
        )
        self.assertEqual(
            push.call_args.kwargs["instance_desired_states"],
            {
                qualified_uid: {
                    "segment_priority_instance_settings": {"segments": []},
                    "segment_super_priority_instance_settings": {
                        "segments": [101, 102],
                        "slots_override": 1,
                    },
                },
            },
        )
        self.assertEqual(
            push.call_args.kwargs["instance_desired_state_revisions"],
            {qualified_uid: 5},
        )
        self.assertEqual(push.call_args.kwargs["desired_state_revision"], 7)

    def test_explicit_local_instance_removal_clears_only_removed_field(self) -> None:
        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
        install = SimpleNamespace(
            instance_uid="generic.sales-snap.com",
            root="/var/www/generic/public_html",
            name="generic",
            primary_domain="generic.sales-snap.com",
            domains=["generic.sales-snap.com"],
        )
        qualified_uid = "generic.sales-snap.com@MauticFarm-02"
        previous = {
            "segment_super_priority_instance_settings": {
                "generic.sales-snap.com": {"segments": [101, 102], "slots_override": 1},
            },
        }
        current = {
            "segment_priority_instance_settings": {
                "generic.sales-snap.com": {"segments": [303]},
            },
        }
        fresh = {
            "status": "ok",
            "runtime_overrides": {},
            "instance_desired_states": {
                qualified_uid: {
                    "revision": 5,
                    "runtime_overrides": {
                        "segment_priority_instance_settings": {"segments": [404]},
                        "segment_super_priority_instance_settings": {
                            "segments": [101, 102],
                            "slots_override": 1,
                        },
                        "segment_whitelist_instance_settings": {"segments": [909]},
                    },
                },
            },
            "desired_state": {"revision": 9},
        }
        with patch("mcd_agent.daemon.fetch_runtime_overrides", return_value=fresh), patch(
            "mcd_agent.daemon.push_runtime_overrides",
            return_value={"status": "ok"},
        ) as push:
            result = daemon._push_local_runtime_change(
                cfg,
                previous,
                current,
                [install],
                desired_revision=8,
                instance_revisions={qualified_uid: 4},
            )

        self.assertEqual(result["status"], "ok")
        row = push.call_args.kwargs["instance_desired_states"][qualified_uid]
        self.assertEqual(row["segment_priority_instance_settings"], {"segments": [303]})
        self.assertEqual(row["segment_whitelist_instance_settings"], {"segments": [909]})
        self.assertNotIn("segment_super_priority_instance_settings", row)
        payload = push.call_args.args[1]
        self.assertEqual(
            payload["segment_priority_instance_settings"][qualified_uid],
            {"segments": [303]},
        )
        self.assertNotIn("segment_super_priority_instance_settings", payload)
        self.assertEqual(
            payload["segment_whitelist_instance_settings"][qualified_uid],
            {"segments": [909]},
        )

    def test_direct_local_instance_add_is_sent_without_dropping_remote_fields(self) -> None:
        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
        install = SimpleNamespace(
            instance_uid="generic.sales-snap.com",
            root="/var/www/generic/public_html",
            name="generic",
            primary_domain="generic.sales-snap.com",
            domains=["generic.sales-snap.com"],
        )
        qualified_uid = "generic.sales-snap.com@MauticFarm-02"
        previous = {"scheduler_host_max_parallel": 4}
        current = {
            "scheduler_host_max_parallel": 4,
            "segment_super_priority_instance_settings": {
                "generic.sales-snap.com": {"segments": [201, 202], "slots_override": 1},
            },
        }
        fresh = {
            "status": "ok",
            "runtime_overrides": {},
            "instance_desired_states": {
                qualified_uid: {
                    "revision": 5,
                    "runtime_overrides": {
                        "segment_whitelist_instance_settings": {"segments": [909]},
                    },
                },
            },
            "desired_state": {"revision": 10},
        }
        with patch("mcd_agent.daemon.fetch_runtime_overrides", return_value=fresh), patch(
            "mcd_agent.daemon.push_runtime_overrides",
            return_value={"status": "ok"},
        ) as push:
            result = daemon._push_local_runtime_change(
                cfg,
                previous,
                current,
                [install],
                desired_revision=9,
                instance_revisions={qualified_uid: 4},
            )

        self.assertEqual(result["status"], "ok")
        row = push.call_args.kwargs["instance_desired_states"][qualified_uid]
        self.assertEqual(
            row["segment_super_priority_instance_settings"],
            {"segments": [201, 202], "slots_override": 1},
        )
        self.assertEqual(row["segment_whitelist_instance_settings"], {"segments": [909]})
        self.assertEqual(
            push.call_args.args[1]["segment_super_priority_instance_settings"][qualified_uid],
            {"segments": [201, 202], "slots_override": 1},
        )

    def test_remote_instance_state_persists_through_restart_without_clearing_push(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
        install = SimpleNamespace(
            instance_uid="generic.sales-snap.com",
            root="/var/www/generic/public_html",
            name="generic",
            primary_domain="generic.sales-snap.com",
            domains=["generic.sales-snap.com"],
        )
        qualified_uid = "generic.sales-snap.com@MauticFarm-02"
        remote_states = {
            qualified_uid: {
                "revision": 5,
                "runtime_overrides": {
                    "segment_special_rings_v1": {
                        "priority": [],
                        "super_priority": {"segments": [101, 102], "slots_override": 1},
                    },
                },
            },
        }

        with TemporaryDirectory() as directory:
            path = Path(directory) / "mcd.toml"
            path.write_text('[profile]\nname = "farm-hiload"\n', encoding="utf-8")
            cfg_loaded = load_config(str(path), allow_recover_from_mcc=False)

            # The persistence call intentionally has no applied host keys. It
            # must still materialize the canonical instance row from MCC.
            daemon._persist_stable_backup_runtime_to_config(
                cfg_loaded,
                [],
                [install],
                instance_states=remote_states,
            )
            restarted = load_config(str(path), allow_recover_from_mcc=False)
            persisted = local_runtime_overrides(restarted)
            self.assertEqual(
                persisted["segment_super_priority_instance_settings"][qualified_uid],
                {"segments": [101, 102], "slots_override": 1},
            )
            self.assertEqual(
                persisted["segment_priority_instance_settings"][qualified_uid],
                {"segments": []},
            )

            merged = merge_instance_desired_states({}, remote_states)
            applied = apply_remote_overrides(restarted, merged)
            self.assertEqual(
                daemon._segment_super_priority_effective_setting(applied["config"], install),
                {101, 102},
            )
            self.assertEqual(
                daemon._segment_static_lane_parallel_limit(applied["config"], install, super_priority=True),
                1,
            )

            with patch("mcd_agent.daemon.fetch_runtime_overrides", return_value={
                "status": "ok",
                "runtime_overrides": {},
                "instance_desired_states": remote_states,
                "desired_state": {"revision": 7},
            }), patch(
                "mcd_agent.daemon.push_runtime_overrides",
                return_value={"status": "ok"},
            ) as push:
                result = daemon._push_local_runtime_change(
                    cfg,
                    persisted,
                    persisted,
                    [install],
                    desired_revision=6,
                    instance_revisions={qualified_uid: 5},
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(
                push.call_args.kwargs["instance_desired_states"][qualified_uid][
                    "segment_super_priority_instance_settings"
                ],
                {"segments": [101, 102], "slots_override": 1},
            )
            self.assertEqual(
                push.call_args.kwargs["instance_desired_state_revisions"],
                {qualified_uid: 5},
            )

    def test_suffixed_mcc_response_maps_rings_and_acks_exact_revision(self) -> None:
        base_uid = "generic.sales-snap.com"
        qualified_uid = f"{base_uid}@MauticFarm-02"
        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
        response = {
            "status": "ok",
            "runtime_overrides": {},
            "instance_desired_states": {
                base_uid: {
                    "revision": 1,
                    "runtime_overrides": {
                        "segment_special_rings_v1": {
                            "priority": [],
                            "super_priority": [],
                        }
                    },
                },
                qualified_uid: {
                    "revision": 5,
                    "runtime_overrides": {
                        "segment_special_rings_v1": {
                            "priority": [],
                            "super_priority": {"segments": [101, 102], "slots_override": 1},
                        }
                    },
                }
            },
        }
        identity = {
            "effective_hostname": "MauticFarm-02",
            "effective_mcc_host_name": "MauticFarm-02",
            "local_hostname": "MauticFarm-02",
            "configured_host_name": "",
        }
        with patch(
            "mcd_agent.runtime_overrides.resolve_agent_identity",
            return_value=identity,
        ), patch("mcd_agent.runtime_overrides._post_json", return_value=response) as post:
            fetched = fetch_runtime_overrides(cfg, instance_uids=[base_uid])

        self.assertEqual(
            post.call_args.args[1]["instance_uids"],
            [base_uid, qualified_uid],
        )
        states = fetched["instance_desired_states"]
        self.assertEqual(list(states), [qualified_uid])
        self.assertEqual(states[qualified_uid]["revision"], 5)
        merged = merge_instance_desired_states(fetched["runtime_overrides"], states)
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "mcd.toml"
            path.write_text('[profile]\nname = "farm-hiload"\n', encoding="utf-8")
            cfg_loaded = load_config(str(path), allow_recover_from_mcc=False)
            applied = apply_remote_overrides(cfg_loaded, merged)

        install = SimpleNamespace(
            instance_uid=base_uid,
            root="/var/www/generic/public_html",
            name="generic",
            primary_domain=base_uid,
            domains=[base_uid],
        )
        effective = applied["config"]
        self.assertEqual(daemon._segment_priority_effective_setting(effective, install), set())
        self.assertEqual(
            daemon._segment_super_priority_effective_setting(effective, install),
            {101, 102},
        )
        self.assertEqual(daemon._segment_static_lane_parallel_limit(effective, install, super_priority=True), 1)

        class MemoryStore:
            def __init__(self):
                self.values = {}

            def get_runtime_sync(self, key):
                return self.values.get(key)

            def put_runtime_sync(self, key, value):
                self.values[key] = dict(value)

        store = MemoryStore()
        with patch(
            "mcd_agent.runtime_overrides.acknowledge_runtime_state",
            return_value={"status": "ok"},
        ) as acknowledge:
            failures = acknowledge_desired_runtime_states(
                cfg,
                store,
                desired_state=None,
                host_runtime={},
                instance_states=states,
                runtime_fingerprint="aggregate",
            )

        self.assertEqual(failures, [])
        self.assertEqual(acknowledge.call_args.kwargs["scope_key"], qualified_uid)
        self.assertEqual(acknowledge.call_args.kwargs["revision"], 5)
        self.assertEqual(
            store.values["mcc_runtime_acknowledged"]["instances"][qualified_uid]["revision"],
            5,
        )

    def test_instance_state_uses_uid_and_reapplies_after_root_move(self) -> None:
        inst = SimpleNamespace(
            instance_uid="app.sales-snap.com",
            root="/var/www/old/public_html",
            name="app",
            primary_domain="app.sales-snap.com",
            domains=["app.sales-snap.com"],
        )
        states = instance_desired_states(
            {
                "segment_whitelist_instance_settings": {
                    "/var/www/old/public_html": {"segment_whitelist": [86]},
                },
                "segment_recurring_priority_v1": {
                    "/var/www/old/public_html": {"segments": [{"id": 86, "max_interval_sec": 60}]},
                },
            },
            [inst],
        )
        self.assertEqual(states["app.sales-snap.com"]["segment_whitelist_instance_settings"]["segment_whitelist"], [86])
        self.assertEqual(states["app.sales-snap.com"]["segment_recurring_priority_v1"]["segments"][0]["id"], 86)
        merged = merge_instance_desired_states(
            {},
            {
                "app.sales-snap.com": {
                    "runtime_overrides": states["app.sales-snap.com"],
                    "revision": 1,
                }
            },
        )
        self.assertEqual(
            merged["segment_whitelist_instance_settings"]["app.sales-snap.com"]["segment_whitelist"],
            [86],
        )
        self.assertEqual(
            merged["segment_recurring_priority_v1"]["app.sales-snap.com"]["segments"][0]["max_interval_sec"],
            60,
        )

    def test_canonical_desired_state_matches_legacy_local_inventory_uid_and_acks_canonical_uid(self) -> None:
        inst = SimpleNamespace(
            instance_uid="electronic.sales-snap.com",
            root="/var/www/electronic/public_html",
            name="electronic.sales-snap.com",
            primary_domain="electronic.sales-snap.com",
            domains=["electronic.sales-snap.com"],
        )
        canonical_uid = "electronic.sales-snap.com@MauticFarm-02"
        states = instance_desired_states(
            {
                "segment_recurring_priority_v1": {
                    canonical_uid: {"segments": [{"id": 86, "max_interval_sec": 60}]},
                }
            },
            [inst],
        )

        self.assertEqual(list(states), [canonical_uid])
        self.assertEqual(states[canonical_uid]["segment_recurring_priority_v1"]["segments"][0]["id"], 86)

    def test_empty_authoritative_instance_state_removes_unset_override_only_for_that_uid(self) -> None:
        merged = merge_instance_desired_states(
            {
                "segment_recurring_priority_v1": {
                    "medtradcom.sales-snap.ru": {"segments": [{"id": 5, "max_interval_sec": 60}]},
                    "other.sales-snap.ru": {"segments": [{"id": 7, "max_interval_sec": 60}]},
                }
            },
            {"medtradcom.sales-snap.ru": {"runtime_overrides": {}, "revision": 2}},
        )

        self.assertEqual(
            merged["segment_recurring_priority_v1"],
            {"other.sales-snap.ru": {"segments": [{"id": 7, "max_interval_sec": 60}]}},
        )

    def test_empty_qualified_instance_state_removes_stale_bare_uid_override(self) -> None:
        merged = merge_instance_desired_states(
            {
                "segment_recurring_priority_v1": {
                    "medtradcom.sales-snap.ru": {"segments": [{"id": 5, "max_interval_sec": 60}]},
                }
            },
            {"medtradcom.sales-snap.ru@alex-personal": {"runtime_overrides": {}, "revision": 2}},
        )

        self.assertNotIn("segment_recurring_priority_v1", merged)

    def test_removed_stable_runtime_key_is_deleted_from_local_config(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "mcd.toml"
            path.write_text(
                '[profile]\nname = "farm-maxi"\n\n[runtime]\n'
                'segment_recurring_priority_v1 = { "medtradcom.sales-snap.ru" = { segments = [{ id = 5, max_interval_sec = 60 }] } }\n',
                encoding="utf-8",
            )

            changed_path, changed = remove_runtime_values(
                str(path),
                {"segment_recurring_priority_v1"},
            )

            self.assertTrue(changed)
            self.assertEqual(changed_path, str(path))
            self.assertNotIn("segment_recurring_priority_v1", path.read_text(encoding="utf-8"))

            rebased = load_config(str(path), allow_recover_from_mcc=False)
            applied = apply_remote_overrides(rebased, {})
            self.assertEqual(applied["config"].segment_recurring_priority_v1, {})

    def test_authoritative_instance_clear_survives_legacy_file_and_restart(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "mcd.toml"
            path.write_text(
                '[profile]\nname = "farm-maxi"\n\n[runtime]\n'
                'segment_recurring_priority_v1 = { "medtradcom.sales-snap.ru" = { segments = [{ id = 5, max_interval_sec = 60 }] } }\n',
                encoding="utf-8",
            )
            legacy = load_config(str(path), allow_recover_from_mcc=False)
            self.assertIn("segment_recurring_priority_v1", local_runtime_overrides(legacy))

            desired = merge_instance_desired_states(
                {},
                {"medtradcom.sales-snap.ru@alex-personal": {"runtime_overrides": {}, "revision": 2}},
            )
            self.assertEqual(desired, {})
            daemon._remove_absent_stable_runtime_from_config(legacy, desired)
            reloaded = load_config(str(path), allow_recover_from_mcc=False)
            effective = apply_remote_overrides(reloaded, desired)["config"]

            self.assertNotIn("segment_recurring_priority_v1", local_runtime_overrides(reloaded))
            self.assertEqual(effective.segment_recurring_priority_v1, {})
            self.assertEqual(
                load_config(str(path), allow_recover_from_mcc=False).segment_recurring_priority_v1,
                {},
            )

    def test_failed_empty_instance_ack_retries_after_restart(self) -> None:
        class MemoryStore:
            def __init__(self, values=None):
                self.values = dict(values or {})

            def get_runtime_sync(self, key):
                return self.values.get(key)

            def put_runtime_sync(self, key, value):
                self.values[key] = dict(value)

        cfg = SimpleNamespace()
        uid = "medtradcom.sales-snap.ru@alex-personal"
        rows = {uid: {"revision": 2, "runtime_overrides": {}}}
        store = MemoryStore()
        with patch(
            "mcd_agent.runtime_overrides.acknowledge_runtime_state",
            side_effect=[{"status": "error", "reason": "temporary_network_error"}, {"status": "ok"}],
        ) as acknowledge:
            failed = acknowledge_desired_runtime_states(
                cfg, store, desired_state=None, host_runtime={},
                instance_states=rows, runtime_fingerprint="aggregate",
            )
            self.assertEqual(len(failed), 1)
            self.assertNotIn("mcc_runtime_acknowledged", store.values)

            restarted = MemoryStore(store.values)
            self.assertEqual(
                acknowledge_desired_runtime_states(
                    cfg, restarted, desired_state=None, host_runtime={},
                    instance_states=rows, runtime_fingerprint="aggregate",
                ),
                [],
            )
            self.assertEqual(
                restarted.values["mcc_runtime_acknowledged"]["instances"][uid],
                {"scope_key": uid, "revision": 2, "content_sha256": overrides_fingerprint({})},
            )
            acknowledge_desired_runtime_states(
                cfg, restarted, desired_state=None, host_runtime={},
                instance_states=rows, runtime_fingerprint="aggregate",
            )
            self.assertEqual(acknowledge.call_count, 2)
            self.assertEqual(acknowledge.call_args.kwargs["revision"], 2)
            self.assertEqual(
                acknowledge.call_args.kwargs["observed"]["content_sha256"],
                overrides_fingerprint({}),
            )

    def test_new_host_revision_is_acked_even_when_value_is_unchanged(self) -> None:
        store = SimpleNamespace(values={})
        store.get_runtime_sync = lambda key: store.values.get(key)
        store.put_runtime_sync = lambda key, value: store.values.__setitem__(key, dict(value))
        cfg = SimpleNamespace()
        with patch("mcd_agent.runtime_overrides.acknowledge_runtime_state", return_value={"status": "ok"}) as acknowledge:
            for revision in (3, 4, 4):
                acknowledge_desired_runtime_states(
                    cfg, store,
                    desired_state={"scope_key": "test-host", "revision": revision},
                    host_runtime={}, instance_states={}, runtime_fingerprint="aggregate",
                )
        self.assertEqual(acknowledge.call_count, 2)
        self.assertEqual(store.values["mcc_runtime_acknowledged"]["host"]["revision"], 4)

    def test_special_rings_map_to_generic_instance_lanes_and_ack_exact_revision(self) -> None:
        class MemoryStore:
            def __init__(self):
                self.values = {}

            def get_runtime_sync(self, key):
                return self.values.get(key)

            def put_runtime_sync(self, key, value):
                self.values[key] = dict(value)

        uid = "generic.sales-snap.com@MauticFarm-02"
        special = {
            "priority": [],
            "super_priority": {"segments": [101, 102], "slots_override": 1},
        }
        states = {uid: {"revision": 5, "runtime_overrides": {"segment_special_rings_v1": special}}}
        merged = merge_instance_desired_states({}, states)

        self.assertNotIn("segment_special_rings_v1", merged)
        self.assertEqual(
            merged["segment_priority_instance_settings"][uid],
            {"segments": []},
        )
        self.assertEqual(
            merged["segment_super_priority_instance_settings"][uid],
            {"segments": [101, 102], "slots_override": 1},
        )

        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as directory:
            path = Path(directory) / "mcd.toml"
            path.write_text('[profile]\nname = "farm-hiload"\n', encoding="utf-8")
            cfg = load_config(str(path), allow_recover_from_mcc=False)
            applied = apply_remote_overrides(cfg, merged)

        effective = applied["config"]
        inst = SimpleNamespace(instance_uid=uid, root="/var/www/generic/public_html")
        self.assertEqual(
            daemon._segment_priority_effective_setting(effective, inst),
            set(),
        )
        self.assertEqual(
            daemon._segment_super_priority_effective_setting(effective, inst),
            {101, 102},
        )
        self.assertEqual(
            daemon._segment_static_lane_parallel_limit(effective, inst, super_priority=True),
            1,
        )
        self.assertNotIn("segment_special_rings_v1", applied["unsupported_keys"])

        store = MemoryStore()
        cfg_for_ack = SimpleNamespace(
            mcc_url="https://mcc.example",
            mcc_token="token",
            profile_name="farm-hiload",
            segment_priority_instance_settings={uid: {"segments": []}},
            segment_super_priority_instance_settings={
                uid: {"segments": [101, 102], "slots_override": 1}
            },
        )
        with patch(
            "mcd_agent.runtime_overrides.acknowledge_runtime_state",
            return_value={"status": "ok"},
        ) as acknowledge:
            failures = acknowledge_desired_runtime_states(
                cfg_for_ack,
                store,
                desired_state=None,
                host_runtime={},
                instance_states=states,
                runtime_fingerprint="aggregate",
            )

        self.assertEqual(failures, [])
        self.assertEqual(acknowledge.call_args.kwargs["revision"], 5)
        self.assertEqual(acknowledge.call_args.kwargs["status"], "applied")
        self.assertEqual(
            store.values["mcc_runtime_acknowledged"]["instances"][uid]["revision"],
            5,
        )

    def test_unknown_special_ring_shape_is_rejected_without_applied_revision(self) -> None:
        class MemoryStore:
            def __init__(self):
                self.values = {}

            def get_runtime_sync(self, key):
                return self.values.get(key)

            def put_runtime_sync(self, key, value):
                self.values[key] = dict(value)

        uid = "generic.sales-snap.com@MauticFarm-02"
        states = {
            uid: {
                "revision": 6,
                "runtime_overrides": {"segment_special_rings_v1": {"super_priority": {"bad": "shape"}}},
            }
        }
        store = MemoryStore()
        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
        with patch(
            "mcd_agent.runtime_overrides.acknowledge_runtime_state",
            return_value={"status": "ok"},
        ) as acknowledge:
            failures = acknowledge_desired_runtime_states(
                cfg,
                store,
                desired_state=None,
                host_runtime={},
                instance_states=states,
                runtime_fingerprint="aggregate",
            )

        self.assertEqual(len(failures), 1)
        self.assertEqual(acknowledge.call_args.kwargs["status"], "rejected")
        self.assertNotIn("mcc_runtime_acknowledged", store.values)

    def test_special_ring_validation_does_not_drop_bad_ids_or_unknown_fields(self) -> None:
        self.assertIsNone(
            normalize_segment_special_rings(
                {"priority": [101, "not-an-id"], "super_priority": []}
            )
        )
        self.assertIsNone(
            normalize_segment_special_rings(
                {"priority": {"segments": [101], "future_key": True}, "super_priority": []}
            )
        )

    def test_runtime_lww_mcd_newer_wins_over_mcc_even_when_mcc_revision_is_higher(self) -> None:
        local_metadata = daemon._runtime_metadata_empty()
        remote_metadata = daemon._runtime_metadata_empty()
        daemon._runtime_metadata_set(
            local_metadata,
            "host",
            "scheduler_host_max_parallel",
            daemon._runtime_metadata_stamp(
                source="mcd", updated_at=200.0, revision=3, event_id="mcd:200"
            ),
        )
        daemon._runtime_metadata_set(
            remote_metadata,
            "host",
            "scheduler_host_max_parallel",
            daemon._runtime_metadata_stamp(
                source="mcc", updated_at=100.0, revision=99, event_id="mcc:100"
            ),
        )

        resolved = daemon._resolve_runtime_snapshots(
            {"scheduler_host_max_parallel": 12},
            {"scheduler_host_max_parallel": 4},
            local_metadata,
            remote_metadata,
        )
        self.assertEqual(resolved["runtime"]["scheduler_host_max_parallel"], 12)
        self.assertIn(("host", "scheduler_host_max_parallel", ""), resolved["local_wins"])

    def test_runtime_lww_mcc_newer_wins_and_offline_updated_at_is_primary(self) -> None:
        local_metadata = daemon._runtime_metadata_empty()
        remote_metadata = daemon._runtime_metadata_empty()
        daemon._runtime_metadata_set(
            local_metadata,
            "instance",
            "segment_priority_instance_settings",
            daemon._runtime_metadata_stamp(
                source="mcd", updated_at=300.0, revision=1, event_id="mcd:offline"
            ),
            "app.example.test",
        )
        daemon._runtime_metadata_set(
            remote_metadata,
            "instance",
            "segment_priority_instance_settings",
            daemon._runtime_metadata_stamp(
                source="mcc", updated_at=200.0, revision=50, event_id="mcc:offline"
            ),
            "app.example.test@farm-a",
        )
        resolved = daemon._resolve_runtime_snapshots(
            {
                "segment_priority_instance_settings": {
                    "app.example.test": {"segments": [101]}
                }
            },
            {
                "segment_priority_instance_settings": {
                    "app.example.test@farm-a": {"segments": [202]}
                }
            },
            local_metadata,
            remote_metadata,
        )
        self.assertEqual(
            resolved["runtime"]["segment_priority_instance_settings"]["app.example.test"],
            {"segments": [101]},
        )

        daemon._runtime_metadata_set(
            remote_metadata,
            "host",
            "scheduler_host_max_parallel",
            daemon._runtime_metadata_stamp(
                source="mcc", updated_at=20.0, revision=8, event_id="mcc:newer"
            ),
        )
        daemon._runtime_metadata_set(
            local_metadata,
            "host",
            "scheduler_host_max_parallel",
            daemon._runtime_metadata_stamp(
                source="mcd", updated_at=10.0, revision=100, event_id="mcd:older"
            ),
        )
        newer_remote = daemon._resolve_runtime_snapshots(
            {"scheduler_host_max_parallel": 12},
            {"scheduler_host_max_parallel": 4},
            local_metadata,
            remote_metadata,
        )
        self.assertEqual(newer_remote["runtime"]["scheduler_host_max_parallel"], 4)

    def test_explicit_newer_remote_tombstone_wins_but_stale_absence_does_not_delete(self) -> None:
        local_metadata = daemon._runtime_metadata_empty()
        daemon._runtime_metadata_set(
            local_metadata,
            "host",
            "scheduler_host_max_parallel",
            daemon._runtime_metadata_stamp(
                source="mcd", updated_at=10.0, revision=1, event_id="mcd:old"
            ),
        )
        stale = daemon._resolve_runtime_snapshots(
            {"scheduler_host_max_parallel": 12},
            {},
            local_metadata,
            daemon._runtime_metadata_empty(),
        )
        self.assertEqual(stale["runtime"]["scheduler_host_max_parallel"], 12)
        self.assertFalse(stale["remote_tombstones"])

        remote_metadata = daemon._runtime_metadata_empty()
        daemon._runtime_metadata_set(
            remote_metadata,
            "host",
            "scheduler_host_max_parallel",
            daemon._runtime_metadata_stamp(
                source="mcc",
                updated_at=20.0,
                revision=2,
                event_id="mcc:delete",
                tombstone=True,
            ),
        )
        deleted = daemon._resolve_runtime_snapshots(
            {"scheduler_host_max_parallel": 12},
            {},
            local_metadata,
            remote_metadata,
        )
        self.assertNotIn("scheduler_host_max_parallel", deleted["runtime"])
        self.assertIn(("host", "scheduler_host_max_parallel", ""), deleted["remote_tombstones"])

    def test_runtime_metadata_and_recovered_instance_row_survive_restart_and_push_once(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        class MemoryStore:
            def __init__(self, values=None):
                self.values = dict(values or {})

            def get_runtime_sync(self, key):
                return self.values.get(key)

            def put_runtime_sync(self, key, value):
                self.values[key] = dict(value)

            def delete_runtime_sync(self, keys):
                for key in keys:
                    self.values.pop(key, None)
                return 1

        row = {"segments": [101], "slots_override": 1}
        local_before_update = {
            "segment_priority_instance_settings": {
                "app.example.test@farm-a": row,
            }
        }
        install = SimpleNamespace(
            instance_uid="app.example.test",
            root="/var/www/app/public",
            name="app",
            primary_domain="app.example.test",
            domains=["app.example.test"],
        )

        with TemporaryDirectory() as directory:
            path = Path(directory) / "mcd.toml"
            path.write_text('[profile]\nname = "farm-hiload"\n', encoding="utf-8")
            cfg = load_config(str(path), allow_recover_from_mcc=False)
            store = MemoryStore({"local_runtime": local_before_update, "mcc_runtime": {}})

            baseline, _ = daemon._initialize_local_runtime_baseline(cfg, store)
            self.assertEqual(baseline, {})
            self.assertIsNotNone(daemon._read_local_runtime_push_pending(store))
            recovered_cfg = load_config(str(path), allow_recover_from_mcc=False)
            recovered = local_runtime_overrides(recovered_cfg)
            self.assertEqual(
                recovered["segment_priority_instance_settings"]["app.example.test@farm-a"],
                row,
            )

            accepted = {
                "status": "ok",
                "desired_state": {"revision": 4},
                "runtime_overrides": {},
                "instance_desired_states": {
                    "app.example.test@farm-a": {"revision": 2, "runtime_overrides": {}}
                },
            }
            with patch("mcd_agent.daemon.fetch_runtime_overrides", return_value={
                "status": "ok",
                "runtime_overrides": {},
                "instance_desired_states": {},
                "desired_state": {"revision": 3},
            }), patch("mcd_agent.daemon.push_runtime_overrides", return_value=accepted) as push:
                result = daemon._push_local_runtime_change(
                    cfg,
                    {},
                    recovered,
                    [install],
                    desired_revision=3,
                    instance_revisions={},
                    runtime_metadata=store.get_runtime_sync(daemon._LOCAL_RUNTIME_METADATA_KEY),
                )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(push.call_count, 1)
            self.assertIn(
                "runtime_change_metadata",
                push.call_args.kwargs,
            )
            self.assertTrue(
                daemon._store_desired_runtime_push_result(
                    store,
                    accepted,
                    accepted_runtime=recovered,
                    runtime_metadata=store.get_runtime_sync(daemon._LOCAL_RUNTIME_METADATA_KEY),
                )
            )
            store.put_runtime_sync("local_runtime", recovered)
            daemon._clear_local_runtime_push_pending(store)
            restarted = load_config(str(path), allow_recover_from_mcc=False)
            restarted_baseline, _ = daemon._initialize_local_runtime_baseline(restarted, store)
            self.assertEqual(restarted_baseline, recovered)
            self.assertIsNone(daemon._read_local_runtime_push_pending(store))
            self.assertEqual(
                store.get_runtime_sync(daemon._LOCAL_RUNTIME_METADATA_KEY)["schema"],
                daemon._RUNTIME_METADATA_SCHEMA,
            )
            self.assertEqual(push.call_count, 1)

    def test_restart_bootstrap_reads_fresh_mcc_row_before_classifying_local_absence(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        class MemoryStore:
            def __init__(self, values=None):
                self.values = dict(values or {})

            def get_runtime_sync(self, key):
                return self.values.get(key)

            def put_runtime_sync(self, key, value):
                self.values[key] = dict(value)

            def delete_runtime_sync(self, keys):
                for key in keys:
                    self.values.pop(key, None)
                return 1

        qualified_uid = "app.example.test@farm-a"
        desired_runtime = {
            "segment_priority_instance_settings": {
                qualified_uid: {"segments": [7], "slots_override": 1}
            },
            "segment_super_priority_instance_settings": {
                qualified_uid: {"segments": [8, 9], "slots_override": 1}
            },
        }
        response = {
            "status": "ok",
            "runtime_overrides": {},
            "instance_desired_states": {
                qualified_uid: {
                    "revision": 4,
                    "updated_at": 200.0,
                    "runtime_overrides": {
                        "segment_priority_instance_settings": {"segments": [7], "slots_override": 1},
                        "segment_super_priority_instance_settings": {
                            "segments": [8, 9],
                            "slots_override": 1,
                        },
                    },
                }
            },
            "desired_state": {"revision": 12, "updated_at": 200.0},
        }

        with TemporaryDirectory() as directory:
            path = Path(directory) / "mcd.toml"
            path.write_text('[profile]\nname = "farm-hiload"\n', encoding="utf-8")
            cfg = SimpleNamespace(
                config_file_path=str(path),
                mcc_url="https://mcc.example",
                mcc_token="token",
            )
            store = MemoryStore(
                {
                    # This is the accepted local state before the simulated
                    # restart; the mutable file itself is intentionally empty.
                    "local_runtime": desired_runtime,
                    "mcc_runtime": {},
                }
            )
            with patch("mcd_agent.daemon.fetch_runtime_overrides", return_value=response) as fetch:
                status = daemon._bootstrap_remote_runtime_before_local_baseline(
                    cfg,
                    store,
                    ["app.example.test"],
                )
            self.assertEqual(status, "applied")
            self.assertEqual(fetch.call_count, 1)

            restarted = load_config(str(path), allow_recover_from_mcc=False)
            persisted = local_runtime_overrides(restarted)
            self.assertEqual(persisted, desired_runtime)
            baseline, _ = daemon._initialize_local_runtime_baseline(restarted, store)
            self.assertEqual(baseline, desired_runtime)
            self.assertIsNone(daemon._read_local_runtime_push_pending(store))
            self.assertEqual(store.get_runtime_sync("mcc_runtime"), desired_runtime)

    def test_selected_profile_provenance_survives_runtime_sync_rebase(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "mcd.toml"
            path.write_text('[profile]\nname = "farm-hiload"\n', encoding="utf-8")
            selected = load_config(str(path), allow_recover_from_mcc=False)
            path.write_text('[profile]\nname = "passive"\n', encoding="utf-8")
            stale = load_config(str(path), allow_recover_from_mcc=False)

            rebased = reapply_profile_authority(stale, selected.profile_name)
            self.assertEqual(rebased.profile_name, "farm-hiload")
            self.assertEqual(rebased.scheduler_host_max_parallel, 12)
            self.assertEqual(rebased.scheduler_instance_max_parallel, 6)

            persist_profile_authority(str(path), selected.profile_name)
            reloaded = load_config(str(path), allow_recover_from_mcc=False)
            self.assertEqual(reloaded.profile_name, "farm-hiload")
            self.assertEqual(reloaded.scheduler_host_max_parallel, 12)

    def test_desired_profile_wins_over_stale_observed_profile(self) -> None:
        self.assertEqual(
            canonical_profile_from_runtime_response(
                {
                    "profile_name": "passive",
                    "desired_state": {"profile": "farm-hiload"},
                    "observed_profile": "passive",
                }
            ),
            "farm-hiload",
        )
        self.assertEqual(
            desired_profile_from_runtime_response(
                {
                    "desired_state": {"profile": "passive"},
                    "observed_profile": "farm-hiload",
                }
            ),
            "passive",
        )


if __name__ == "__main__":
    unittest.main()
