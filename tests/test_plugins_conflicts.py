from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import mcd_agent.state_backend as state_backend
from mcd_agent.plugins import (
    _auto_remove_conflicting_installed_bundles,
    _apply_plugin_file_changes,
    _cluster_plugin_cache_clear_all,
    _cluster_pre_sql_is_dangerous,
    _cluster_plugin_note_node,
    _cluster_plugin_local_is_reference,
    _cluster_plugin_reference_host,
    _cluster_plugin_row_signature,
    _cluster_plugin_wait_file_sync,
    _run_cluster_plugin_operation,
    _run_plugin_install_reload,
    _plugin_selection_digest,
    _protected_plugin_path_names,
    _remove_plugin_path,
    _apply_plugin_config_metadata_patch,
    _run_manifest_sql_fixes,
    _exclusive_counterparts,
)


class PluginConflictPathTests(unittest.TestCase):
    def test_selected_install_bundle_is_protected_from_conflict_aliases(self) -> None:
        selected = [
            {
                "bundle": "AmazonSesBundle",
                "install_bundle": "AmazonSesBundle",
                "item": {
                    "replaces": ["AmazonSesBundleDev", "AmazonSesOriginalBundle"],
                },
            }
        ]

        self.assertEqual(
            _auto_remove_conflicting_installed_bundles(selected, Path("/tmp/plugins")),
            ["AmazonSesBundleDev", "AmazonSesOriginalBundle"],
        )
        self.assertEqual(_protected_plugin_path_names(selected), {"AmazonSesBundle"})

    def test_conflict_cleanup_does_not_delete_selected_runtime_path(self) -> None:
        selected = [
            {
                "bundle": "AmazonSesBundle",
                "install_bundle": "AmazonSesBundle",
                "item": {
                    "replaces": ["AmazonSesBundleDev"],
                },
            }
        ]
        protected = _protected_plugin_path_names(selected)

        with TemporaryDirectory() as tmp:
            plugins_dir = Path(tmp)
            selected_path = plugins_dir / "AmazonSesBundle"
            selected_path.mkdir()

            # AmazonSesBundleDev is a manifest variant that installs into the same
            # runtime directory. Auto-clean must not remove the selected path.
            removed = []
            for name in {"AmazonSesBundleDev", "AmazonSesBundle"}:
                if name in protected:
                    continue
                if _remove_plugin_path(plugins_dir / name):
                    removed.append(name)

            self.assertEqual(removed, [])
            self.assertTrue(selected_path.is_dir())

    def test_amazon_ses_conflicts_with_sns_callback_implementations(self) -> None:
        self.assertEqual(
            _exclusive_counterparts("AmazonSesBundle"),
            {"AmazonSesBundleDev", "AmazonSnsCallbackBundle", "MauticAmazonSesBundle"},
        )
        self.assertIn("AmazonSesBundle", _exclusive_counterparts("AmazonSnsCallbackBundle"))
        self.assertIn("AmazonSesBundle", _exclusive_counterparts("MauticAmazonSesBundle"))

    def test_cluster_plugin_reference_is_first_cache_host(self) -> None:
        cfg = SimpleNamespace(
            cluster_id="cluster-a",
            cluster_routing_enabled=True,
            cluster_route_cache_hosts=["host-a", "host-b"],
            cluster_node_index=1,
            mcc_host_name="host-a",
        )

        self.assertEqual(_cluster_plugin_reference_host(cfg), "host-a")
        self.assertTrue(_cluster_plugin_local_is_reference(cfg))

    def test_plugin_selection_digest_tracks_bundle_content(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "plugins" / "DemoBundle"
            bundle.mkdir(parents=True)
            (bundle / "DemoBundle.php").write_text("<?php\n", encoding="utf-8")

            first = _plugin_selection_digest(str(root), ["DemoBundle"])
            (bundle / "Config").mkdir()
            (bundle / "Config" / "config.php").write_text("<?php return [];\n", encoding="utf-8")
            second = _plugin_selection_digest(str(root), ["DemoBundle"])

            self.assertEqual(first["status"], "ok")
            self.assertEqual(second["status"], "ok")
            self.assertNotEqual(first["digest"], second["digest"])

    def test_remove_action_deletes_manifest_bundle_and_install_alias(self) -> None:
        cfg = SimpleNamespace(plugins_post_cache_clear=False, plugins_post_install=False)
        install = SimpleNamespace(root="", db=None, mautic_major=6)
        selected = [
            {
                "bundle": "DemoBundleDev",
                "install_bundle": "DemoBundle",
                "item": {"bundle": "DemoBundleDev"},
                "status": "OK",
            }
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugins = root / "plugins"
            (plugins / "DemoBundle").mkdir(parents=True)
            (plugins / "DemoBundleDev").mkdir(parents=True)

            changed = _apply_plugin_file_changes(
                config=cfg,
                install=install,
                install_root=str(root),
                manifest_dir="https://mcc.example/plugins/",
                fallback_ip=None,
                action="remove",
                selected=selected,
                auto_remove_bundles=[],
                rows_by_bundle={},
                run_post_steps=False,
            )

            self.assertTrue(changed)
            self.assertFalse((plugins / "DemoBundle").exists())
            self.assertFalse((plugins / "DemoBundleDev").exists())

    def test_cluster_plugin_signature_changes_with_package_version(self) -> None:
        base = [
            {
                "bundle": "DemoBundle",
                "install_bundle": "DemoBundle",
                "plugin_uid": "demo:6",
                "server_version": "1.0.0",
                "item": {"version": "1.0.0", "sha256": "a"},
            }
        ]
        changed = [
            {
                "bundle": "DemoBundle",
                "install_bundle": "DemoBundle",
                "plugin_uid": "demo:6",
                "server_version": "1.0.1",
                "item": {"version": "1.0.1", "sha256": "b"},
            }
        ]

        self.assertNotEqual(
            _cluster_plugin_row_signature("update", base, []),
            _cluster_plugin_row_signature("update", changed, []),
        )

    def test_cluster_pre_sql_blocks_dangerous_ddl_before_db_call(self) -> None:
        cfg = SimpleNamespace(cluster_id="cluster-a")
        install = SimpleNamespace(root="/var/www/ss/public_html", db={"driver": "pdo_mysql"})
        rows = [{"bundle": "DemoBundle", "item": {"pre_sql": ["ALTER TABLE ss_leads ADD demo INT"]}}]

        with patch("mcd_agent.plugins.MauticDB") as db_cls:
            with self.assertRaisesRegex(RuntimeError, "blocked in cluster mode"):
                _run_manifest_sql_fixes(cfg, install, rows)
            db_cls.assert_not_called()

    def test_cluster_pre_sql_blocks_ddl_after_comments_and_multi_statement(self) -> None:
        self.assertTrue(
            _cluster_pre_sql_is_dangerous(
                "/* plugin migration */\nALTER TABLE ss_leads ADD demo INT"
            )
        )
        self.assertTrue(
            _cluster_pre_sql_is_dangerous(
                "UPDATE ss_plugins SET is_published = 1; ALTER TABLE ss_leads ADD demo INT"
            )
        )
        self.assertTrue(
            _cluster_pre_sql_is_dangerous(
                "/*!50000 ALTER TABLE ss_leads ADD demo INT */"
            )
        )
        self.assertFalse(
            _cluster_pre_sql_is_dangerous(
                "-- normal DML\nUPDATE ss_plugins SET is_published = 1 WHERE bundle = 'DemoBundle'"
            )
        )

    def test_cluster_pre_sql_allows_safe_dml(self) -> None:
        cfg = SimpleNamespace(cluster_id="cluster-a")
        install = SimpleNamespace(root="/var/www/ss/public_html", db={"driver": "pdo_mysql"})
        rows = [{"bundle": "DemoBundle", "item": {"pre_sql": ["UPDATE ss_plugins SET is_published = 1"]}}]

        db = SimpleNamespace(execute_sql_template=Mock(return_value=1))
        with patch("mcd_agent.plugins.MauticDB", return_value=db):
            _run_manifest_sql_fixes(cfg, install, rows)

        db.execute_sql_template.assert_called_once_with("UPDATE ss_plugins SET is_published = 1")

    def test_plugin_metadata_repair_skips_when_column_is_absent(self) -> None:
        cfg = SimpleNamespace()
        install = SimpleNamespace(root="/var/www/ss/public_html", db={"driver": "pdo_mysql"})
        db = SimpleNamespace(
            table_has_column=Mock(return_value=False),
            execute_sql_template=Mock(return_value=1),
        )

        with patch("mcd_agent.plugins.MauticDB", return_value=db), \
             patch("mcd_agent.plugins._run_plugin_template", return_value=(0, "ok")) as run_template:
            _run_plugin_install_reload(cfg, install)

        db.table_has_column.assert_called_once_with("{prefix}plugins", "metadata")
        db.execute_sql_template.assert_not_called()
        run_template.assert_called_once()

    def test_plugin_config_metadata_patch_adds_metadata_without_core_patch(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "plugins" / "DemoBundle" / "Config" / "config.php"
            config.parent.mkdir(parents=True)
            config.write_text(
                "<?php\nreturn [\n"
                "    \"name\"        => \"Demo\",\n"
                "    \"version\"     => \"1.0.0\",\n"
                "    \"author\"      => \"Test\",\n"
                "];\n",
                encoding="utf-8",
            )
            install = SimpleNamespace(root=str(root))
            selected = [{"bundle": "DemoBundle", "install_bundle": "DemoBundle", "item": {}}]

            changed = _apply_plugin_config_metadata_patch(install, selected)

            self.assertTrue(changed)
            text = config.read_text(encoding="utf-8")
            self.assertIn('"metadata"    => []', text)
            self.assertFalse((root / "app" / "bundles" / "PluginBundle" / "Helper" / "ReloadHelper.php").exists())

    def test_cluster_node_status_is_written_to_node_scoped_runtime_row(self) -> None:
        calls = []

        def fake_mutate(config, key, mutator, *, host_name=None):
            payload = {"nodes": {"old": {"status": "stale"}}}
            mutator(payload)
            calls.append((host_name, payload))

        with patch("mcd_agent.plugins._cluster_plugin_mutate", side_effect=fake_mutate):
            _cluster_plugin_note_node(
                SimpleNamespace(cluster_id="cluster-a"),
                key="op-key",
                host="host-b",
                status="files_synced",
                message="ok",
                digest="abc",
            )

        self.assertEqual(calls[0][0], "host-b")
        self.assertEqual(calls[0][1]["kind"], "plugin_node_status")
        self.assertEqual(calls[0][1]["status"], "files_synced")
        self.assertEqual(calls[0][1]["digest"], "abc")
        self.assertNotIn("nodes", calls[0][1])

    def test_cluster_file_sync_checks_reference_locally_without_manual_request(self) -> None:
        cfg = SimpleNamespace(
            cluster_id="cluster-a",
            plugins_cluster_file_sync_wait_sec=30,
            cluster_routing_enabled=True,
            cluster_route_cache_hosts=["host-a", "host-b"],
            cluster_node_index=1,
            mcc_host_name="host-a",
        )
        install = SimpleNamespace(root="/var/www/ss/public_html")

        with patch("mcd_agent.plugins._plugin_selection_digest", return_value={"digest": "abc", "status": "ok"}), \
             patch("mcd_agent.plugins._cluster_plugin_set_phase"), \
             patch("mcd_agent.plugins._cluster_plugin_enqueue_manual_request", return_value=42) as enqueue, \
             patch("mcd_agent.plugins._wait_manual_requests", return_value=(True, {("host-b", 42): "done"})) as wait, \
             patch("mcd_agent.plugins._cluster_plugin_read_payload", return_value={"status": "files_synced", "digest": "abc"}), \
             patch("mcd_agent.plugins._cluster_plugin_note_node") as note:
            _cluster_plugin_wait_file_sync(
                config=cfg,
                install=install,
                key="op-key",
                request_hash="req",
                expected_hosts=["host-a", "host-b"],
                reference_host="host-a",
                sync_bundles=["DemoBundle"],
                expected_digest="abc",
            )

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs["target_host_name"], "host-b")
        wait.assert_called_once()
        note.assert_called_once()
        self.assertEqual(note.call_args.kwargs["host"], "host-a")

    def test_cluster_cache_clear_runs_reference_locally_without_manual_request(self) -> None:
        cfg = SimpleNamespace(
            cluster_id="cluster-a",
            plugins_cluster_cache_clear_wait_sec=60,
            php_bin="/usr/bin/php",
            mautic_run_as_user="www-data",
            cluster_routing_enabled=True,
            cluster_route_cache_hosts=["host-a", "host-b"],
            cluster_node_index=1,
            mcc_host_name="host-a",
        )
        install = SimpleNamespace(root="/var/www/ss/public_html")

        with patch("mcd_agent.plugins._run_plugin_cache_clear") as local_clear, \
             patch("mcd_agent.plugins.build_mautic_exec_args", return_value=["sudo", "-u", "www-data", "php", "bin/console", "cache:clear"]), \
             patch("mcd_agent.plugins._cluster_plugin_enqueue_manual_request", return_value=43) as enqueue, \
             patch("mcd_agent.plugins._wait_manual_requests", return_value=(True, {("host-b", 43): "done"})) as wait, \
             patch("mcd_agent.plugins._cluster_plugin_set_phase"):
            _cluster_plugin_cache_clear_all(
                config=cfg,
                install=install,
                key="op-key",
                request_hash="req",
                expected_hosts=["host-a", "host-b"],
                reference_host="host-a",
            )

        local_clear.assert_called_once_with(cfg, install)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs["target_host_name"], "host-b")
        wait.assert_called_once()

    def test_non_reference_cluster_plugin_operation_delegates_without_local_apply(self) -> None:
        cfg = SimpleNamespace(cluster_id="cluster-a")
        install = SimpleNamespace(root="/var/www/ss/public_html")
        selected = [{"bundle": "DemoBundle", "install_bundle": "DemoBundle", "item": {"version": "1.0.0"}}]

        with patch("mcd_agent.plugins.mysql_state_enabled", return_value=True), \
             patch("mcd_agent.plugins._cluster_plugin_reference_host", return_value="host-a"), \
             patch("mcd_agent.plugins._cluster_local_host_name", return_value="host-b"), \
             patch("mcd_agent.plugins._cluster_plugin_expected_hosts", return_value=["host-a", "host-b"]), \
             patch("mcd_agent.plugins._cluster_plugin_local_is_reference", return_value=False), \
             patch("mcd_agent.plugins._cluster_plugin_delegate_to_reference", return_value=0) as delegate, \
             patch("mcd_agent.plugins._apply_plugin_file_changes") as apply_local:
            rc = _run_cluster_plugin_operation(
                config=cfg,
                install=install,
                install_root=install.root,
                manifest_dir="https://mcc.example/manifest/",
                fallback_ip=None,
                action="update",
                selected=selected,
                auto_remove_bundles=[],
                rows_by_bundle={},
            )

        self.assertEqual(rc, 0)
        delegate.assert_called_once()
        apply_local.assert_not_called()

    def test_cluster_remove_verifies_sync_even_when_reference_already_absent(self) -> None:
        cfg = SimpleNamespace(
            cluster_id="cluster-a",
            plugins_post_cache_clear=False,
            plugins_post_install=False,
        )
        install = SimpleNamespace(root="/var/www/ss/public_html")
        selected = [{"bundle": "DemoBundle", "install_bundle": "DemoBundle", "item": {"version": "1.0.0"}}]

        with patch("mcd_agent.plugins.mysql_state_enabled", return_value=True), \
             patch("mcd_agent.plugins._cluster_plugin_reference_host", return_value="host-a"), \
             patch("mcd_agent.plugins._cluster_local_host_name", return_value="host-a"), \
             patch("mcd_agent.plugins._cluster_plugin_expected_hosts", return_value=["host-a", "host-b"]), \
             patch("mcd_agent.plugins._cluster_plugin_local_is_reference", return_value=True), \
             patch("mcd_agent.plugins._cluster_plugin_begin_reference", return_value={"action": "execute"}), \
             patch("mcd_agent.plugins._cluster_plugin_set_phase"), \
             patch("mcd_agent.plugins._apply_plugin_file_changes", return_value=False), \
             patch("mcd_agent.plugins._cluster_sync_bundle_names", return_value=["DemoBundle"]), \
             patch("mcd_agent.plugins._plugin_selection_digest", return_value={"digest": "missing-digest", "status": "ok"}), \
             patch("mcd_agent.plugins._cluster_plugin_wait_file_sync") as wait_sync:
            rc = _run_cluster_plugin_operation(
                config=cfg,
                install=install,
                install_root=install.root,
                manifest_dir="https://mcc.example/manifest/",
                fallback_ip=None,
                action="remove",
                selected=selected,
                auto_remove_bundles=[],
                rows_by_bundle={},
            )

        self.assertEqual(rc, 0)
        wait_sync.assert_called_once()

    def test_existing_state_connection_does_not_ensure_schema(self) -> None:
        sentinel = object()
        cfg = SimpleNamespace()

        with patch.object(state_backend, "_mysql_preflight", return_value=(True, "ok")) as preflight, \
             patch.object(state_backend, "_mysql_conn", return_value=sentinel) as mysql_conn, \
             patch.object(state_backend, "ensure_mysql_state_ready") as ensure_ready, \
             patch.object(state_backend, "_mysql_backoff_clear") as backoff_clear:
            conn = state_backend.mysql_state_existing_connection(cfg)

        self.assertIs(conn, sentinel)
        preflight.assert_called_once_with(cfg)
        mysql_conn.assert_called_once_with(cfg)
        backoff_clear.assert_called_once_with(cfg)
        ensure_ready.assert_not_called()


if __name__ == "__main__":
    unittest.main()
