from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import mcd_agent.self_update as self_update


def _cfg(tmp: str) -> SimpleNamespace:
    return SimpleNamespace(
        state_db_path=str(Path(tmp) / "state.db"),
        mcd_update_policy="approved",
        mcd_update_channel="approved",
        mcd_auto_update_enabled=True,
    )


class SelfUpdateVersionReportingTests(unittest.TestCase):
    def test_update_status_overwrites_stale_state_versions(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            state_path = Path(tmp) / "mcd-self-update.json"
            state_path.write_text(
                json.dumps(
                    {
                        "current_version": "0.9.75",
                        "running_version": "0.9.75",
                        "source_version": "0.9.75",
                        "version_mismatch": False,
                    }
                ),
                encoding="utf-8",
            )
            old_payload = self_update.agent_version_payload
            try:
                self_update.agent_version_payload = lambda: {
                    "agent_version": "0.9.75",
                    "agent_running_version": "0.9.75",
                    "agent_installed_version": "0.9.37",
                    "agent_source_version": "0.9.37",
                    "agent_version_mismatch": True,
                }
                out = self_update.update_status(cfg)
            finally:
                self_update.agent_version_payload = old_payload

        self.assertEqual(out["current_version"], "0.9.75")
        self.assertEqual(out["running_version"], "0.9.75")
        self.assertEqual(out["installed_version"], "0.9.37")
        self.assertEqual(out["source_version"], "0.9.37")
        self.assertTrue(out["version_mismatch"])

    def test_update_status_hides_stale_active_campaign_processes(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            state_path = Path(tmp) / "mcd-self-update.json"
            state_path.write_text(
                json.dumps(
                    {
                        "last_status": "success",
                        "active_campaign_processes": [
                            {"pid": 123, "elapsed_sec": 999, "cmd": "php bin/console mautic:campaigns:trigger"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            old_active = self_update._active_campaign_processes
            try:
                self_update._active_campaign_processes = lambda: []
                out = self_update.update_status(cfg)
            finally:
                self_update._active_campaign_processes = old_active

        self.assertNotIn("active_campaign_processes", out)

    def test_update_status_normalizes_stale_cluster_wait_after_success(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            state_path = Path(tmp) / "mcd-self-update.json"
            state_path.write_text(
                json.dumps(
                    {
                        "last_status": "success",
                        "last_target": "0.9.98",
                        "last_cluster_update_result": "cluster update: waiting for host-b install",
                    }
                ),
                encoding="utf-8",
            )
            old_payload = self_update.agent_version_payload
            try:
                self_update.agent_version_payload = lambda: {
                    "agent_version": "0.9.98",
                    "agent_running_version": "0.9.98",
                    "agent_installed_version": "0.9.98",
                    "agent_source_version": "0.9.98",
                    "agent_version_mismatch": False,
                }
                out = self_update.update_status(cfg)
            finally:
                self_update.agent_version_payload = old_payload

        self.assertEqual(
            out["last_cluster_update_result"],
            "update applied -> 0.9.98; source switched, service restart scheduled",
        )

    def test_apply_update_releases_session_when_only_restart_is_needed(self) -> None:
        releases: list[dict[str, str]] = []
        restarts: list[bool] = []
        with TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            old_installed = self_update.installed_agent_version
            old_pkg_sync = self_update._agent_package_sync_needed
            old_release = self_update.release_session
            old_restart = self_update._restart_service_async
            try:
                self_update.installed_agent_version = lambda: "9.9.9"
                self_update._agent_package_sync_needed = lambda *_args, **_kw: False
                self_update.release_session = lambda _cfg, session_id, **kw: releases.append(
                    {"session_id": session_id, **{k: str(v) for k, v in kw.items()}}
                )
                self_update._restart_service_async = lambda: restarts.append(True)
                ok, msg = self_update.apply_update(
                    cfg,
                    {
                        "status": "update",
                        "target": "9.9.9",
                        "package_url": "https://mcc.invalid/mcd-agent-9.9.9.tar.gz",
                        "session_id": "sess-1",
                    },
                )
            finally:
                self_update.installed_agent_version = old_installed
                self_update._agent_package_sync_needed = old_pkg_sync
                self_update.release_session = old_release
                self_update._restart_service_async = old_restart

            state = json.loads((Path(tmp) / "mcd-self-update.json").read_text(encoding="utf-8"))

        self.assertTrue(ok)
        self.assertIn("service restart scheduled", msg)
        self.assertEqual(restarts, [True])
        self.assertEqual(releases[0]["session_id"], "sess-1")
        self.assertEqual(releases[0]["result_status"], "success")
        self.assertEqual(releases[0]["new_version"], "9.9.9")
        self.assertEqual(state["last_status"], "version_mismatch_restart")

    def test_apply_update_defers_manual_restart_while_backup_lock_is_active(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            cfg.backup_enabled = True
            restarts: list[bool] = []
            old_installed = self_update.installed_agent_version
            old_lock = self_update.backup_lock_active
            old_restart = self_update._restart_service_async
            try:
                self_update.installed_agent_version = lambda: "0.9.252"
                self_update.backup_lock_active = lambda _cfg: True
                self_update._restart_service_async = lambda: restarts.append(True)
                ok, msg = self_update.apply_update(
                    cfg,
                    {
                        "status": "update",
                        "target": "0.9.253",
                        "package_url": "https://mcc.invalid/mcd-agent-0.9.253.tar.gz",
                    },
                )
            finally:
                self_update.installed_agent_version = old_installed
                self_update.backup_lock_active = old_lock
                self_update._restart_service_async = old_restart

            state = json.loads((Path(tmp) / "mcd-self-update.json").read_text(encoding="utf-8"))

        self.assertFalse(ok)
        self.assertEqual(msg, "MCD update deferred: backup lock is active")
        self.assertEqual(state["last_status"], "deferred_backup_lock")
        self.assertEqual(restarts, [])

    def test_apply_update_refreshes_cluster_result_message(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            cfg.cluster_id = "cluster-test"
            cfg.state_mysql_host = "localhost"
            cfg.mcd_install_dir = str(Path(tmp) / "mcd")
            cfg.mcd_update_cleanup_enabled = True
            cfg.mcd_update_cleanup_interval_sec = 86400
            install_dir = Path(cfg.mcd_install_dir)
            (install_dir / "src").mkdir(parents=True)
            state_path = Path(tmp) / "mcd-self-update.json"
            state_path.write_text(
                json.dumps(
                    {
                        "last_cluster_update_result": "cluster update: waiting for host-b install",
                        "active_campaign_processes": [
                            {"pid": 123, "elapsed_sec": 999, "cmd": "php bin/console mautic:campaigns:trigger"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            old_installed = self_update.installed_agent_version
            old_cluster_enabled = self_update._cluster_update_enabled
            old_cluster_host = self_update._cluster_local_host_name
            old_finalize_download = self_update._cluster_update_finalize_download
            old_finalize_install = self_update._cluster_update_finalize_install
            old_release = self_update.release_session
            old_archive_path = self_update._update_archive_path
            old_ensure_archive = self_update._ensure_update_archive
            old_extract = self_update._extract_archive_to_dir
            old_install_reqs = self_update._install_requirements_for_staged_source
            old_install_pkg = self_update._install_agent_package_for_source
            old_smoke = self_update._pre_switch_smoke_check
            old_restart = self_update._restart_service_async
            old_cleanup = self_update._cleanup_update_artifacts
            try:
                self_update.installed_agent_version = lambda: "0.9.95"
                self_update._cluster_update_enabled = lambda _cfg: True
                self_update._cluster_local_host_name = lambda _cfg: "host-a"
                self_update._cluster_update_finalize_download = lambda *args, **kw: None
                self_update._cluster_update_finalize_install = lambda *args, **kw: None
                self_update.release_session = lambda *args, **kw: None
                self_update._update_archive_path = lambda _target: Path(tmp) / "archive.tar.gz"
                self_update._ensure_update_archive = lambda _cfg, _plan: Path(tmp) / "archive.tar.gz"
                self_update._extract_archive_to_dir = lambda _archive, dst: dst.mkdir(parents=True, exist_ok=True)
                self_update._install_requirements_for_staged_source = lambda *_args, **_kw: None
                self_update._install_agent_package_for_source = lambda *_args, **_kw: None
                self_update._pre_switch_smoke_check = lambda *_args, **_kw: None
                self_update._restart_service_async = lambda: None
                self_update._cleanup_update_artifacts = lambda *_args, **_kw: {
                    "archives": 0,
                    "preupdate_backups": 0,
                    "stale_dirs": 0,
                }
                ok, msg = self_update.apply_update(
                    cfg,
                    {
                        "status": "update",
                        "target": "0.9.97",
                        "package_url": "https://mcc.invalid/mcd-agent-0.9.97.tar.gz",
                    },
                )
            finally:
                self_update.installed_agent_version = old_installed
                self_update._cluster_update_enabled = old_cluster_enabled
                self_update._cluster_local_host_name = old_cluster_host
                self_update._cluster_update_finalize_download = old_finalize_download
                self_update._cluster_update_finalize_install = old_finalize_install
                self_update.release_session = old_release
                self_update._update_archive_path = old_archive_path
                self_update._ensure_update_archive = old_ensure_archive
                self_update._extract_archive_to_dir = old_extract
                self_update._install_requirements_for_staged_source = old_install_reqs
                self_update._install_agent_package_for_source = old_install_pkg
                self_update._pre_switch_smoke_check = old_smoke
                self_update._restart_service_async = old_restart
                self_update._cleanup_update_artifacts = old_cleanup

            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertTrue(ok)
        self.assertIn("update applied -> 0.9.97", msg)
        self.assertEqual(
            state["last_cluster_update_result"],
            "update applied -> 0.9.97; source switched, service restart scheduled",
        )
        self.assertNotIn("active_campaign_processes", state)

    def test_apply_update_repairs_stale_venv_package_when_source_is_current(self) -> None:
        repairs: list[tuple[str, str]] = []
        restarts: list[bool] = []
        releases: list[dict[str, str]] = []
        with TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            cfg.mcd_install_dir = str(Path(tmp) / "mcd")
            src = Path(cfg.mcd_install_dir) / "src"
            src.mkdir(parents=True)
            old_installed = self_update.installed_agent_version
            old_pkg_sync = self_update._agent_package_sync_needed
            old_install_pkg = self_update._install_agent_package_for_source
            old_release = self_update.release_session
            old_restart = self_update._restart_service_async
            try:
                self_update.installed_agent_version = lambda: "0.9.153"
                self_update._agent_package_sync_needed = lambda *_args, **_kw: True
                self_update._install_agent_package_for_source = lambda install_dir, source_dir: repairs.append(
                    (str(install_dir), str(source_dir))
                )
                self_update.release_session = lambda _cfg, session_id, **kw: releases.append(
                    {"session_id": session_id, **{k: str(v) for k, v in kw.items()}}
                )
                self_update._restart_service_async = lambda: restarts.append(True)
                ok, msg = self_update.apply_update(
                    cfg,
                    {
                        "status": "update",
                        "target": "0.9.153",
                        "package_url": "https://mcc.invalid/mcd-agent-0.9.153.tar.gz",
                        "session_id": "sess-2",
                    },
                )
            finally:
                self_update.installed_agent_version = old_installed
                self_update._agent_package_sync_needed = old_pkg_sync
                self_update._install_agent_package_for_source = old_install_pkg
                self_update.release_session = old_release
                self_update._restart_service_async = old_restart

        self.assertTrue(ok)
        self.assertIn("agent package sync repaired", msg)
        self.assertEqual(repairs, [(str(Path(cfg.mcd_install_dir)), str(src))])
        self.assertEqual(restarts, [True])
        self.assertEqual(releases[0]["result_status"], "success")

    def test_update_cleanup_defaults_remove_old_agent_artifacts(self) -> None:
        with TemporaryDirectory() as tmp:
            install_dir = Path(tmp) / "mcd"
            updates = install_dir / "var" / "updates"
            backups = install_dir / "var" / "backup"
            updates.mkdir(parents=True)
            backups.mkdir(parents=True)
            (updates / "mcd-agent-0.9.90.tar.gz").write_text("old", encoding="utf-8")
            (updates / "mcd-agent-0.9.94.tar.gz").write_text("current", encoding="utf-8")
            (updates / "src.prev-123").mkdir()
            (backups / "mcd-src-preupdate-123.tgz").write_text("backup", encoding="utf-8")
            cfg = SimpleNamespace(
                mcd_update_keep_archives=0,
                mcd_update_keep_preupdate_backups=0,
                mcd_update_artifacts_max_age_days=1,
            )

            removed = self_update._cleanup_update_artifacts(cfg, now_s=4_000_000_000, install_dir=install_dir)

        self.assertEqual(removed["archives"], 2)
        self.assertEqual(removed["preupdate_backups"], 1)
        self.assertEqual(removed["stale_dirs"], 1)


if __name__ == "__main__":
    unittest.main()
