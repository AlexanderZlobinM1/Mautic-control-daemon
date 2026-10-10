from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import mcd_agent.self_update as self_update


def _cfg(tmp: str) -> SimpleNamespace:
    return SimpleNamespace(
        state_db_path=str(Path(tmp) / "state.db"),
        mcd_update_policy="approved",
        mcd_update_channel="approved",
        mcd_auto_update_enabled=True,
    )


class SelfUpdateVersionReportingTests(unittest.TestCase):
    def test_active_mautic_detector_includes_segment_updates(self) -> None:
        ps = SimpleNamespace(
            returncode=0,
            stdout=(
                " 321  42 /usr/bin/php /var/www/s/public_html/bin/console "
                "mautic:segments:update -i 108 --no-interaction\n"
            ),
            stderr="",
        )
        with patch.object(self_update.subprocess, "run", return_value=ps):
            rows = self_update._active_mautic_processes()

        self.assertEqual(rows[0]["pid"], 321)
        self.assertIn("mautic:segments:update", rows[0]["cmd"])

    def test_apply_update_defers_for_any_active_mautic_console_process(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            releases: list[dict[str, str]] = []
            old_active = self_update._active_mautic_processes
            old_installed = self_update.installed_agent_version
            old_release = self_update.release_session
            try:
                self_update._active_mautic_processes = lambda: [
                    {"pid": 321, "elapsed_sec": 42, "cmd": "php bin/console mautic:segments:update -i 108"}
                ]
                self_update.installed_agent_version = lambda: "1.2.44"
                self_update.release_session = lambda _cfg, session_id, **kw: releases.append(
                    {"session_id": session_id, **{key: str(value) for key, value in kw.items()}}
                )
                ok, msg = self_update.apply_update(
                    cfg,
                    {
                        "status": "update",
                        "target": "1.2.45",
                        "package_url": "https://mcc.invalid/mcd-agent-1.2.45.tar.gz",
                        "session_id": "session-1",
                    },
                )
            finally:
                self_update._active_mautic_processes = old_active
                self_update.installed_agent_version = old_installed
                self_update.release_session = old_release

            state = json.loads((Path(tmp) / "mcd-self-update.json").read_text(encoding="utf-8"))

        self.assertFalse(ok)
        self.assertIn("active Mautic console process", msg)
        self.assertEqual(state["last_status"], "deferred_active_mautic_task")
        self.assertEqual(state["active_mautic_processes"][0]["pid"], 321)
        self.assertEqual(releases[0]["result_status"], "deferred")

    def test_restart_only_repair_is_deferred_for_active_mautic_process(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            old_active = self_update._active_mautic_processes
            old_installed = self_update.installed_agent_version
            old_sync = self_update._agent_package_sync_needed
            try:
                self_update._active_mautic_processes = lambda: [
                    {"pid": 321, "elapsed_sec": 42, "cmd": "php bin/console mautic:segments:update -i 108"}
                ]
                self_update.installed_agent_version = lambda: "9.9.9"
                self_update._agent_package_sync_needed = lambda *_args, **_kw: False
                ok, msg = self_update.apply_update(
                    cfg,
                    {
                        "status": "update",
                        "target": "9.9.8",
                        "package_url": "https://mcc.invalid/mcd-agent-9.9.8.tar.gz",
                    },
                )
            finally:
                self_update._active_mautic_processes = old_active
                self_update.installed_agent_version = old_installed
                self_update._agent_package_sync_needed = old_sync

        self.assertFalse(ok)
        self.assertIn("active Mautic console process", msg)

    def test_daemon_startup_runs_service_unit_migration(self) -> None:
        daemon_source = Path(self_update.__file__).with_name("daemon.py").read_text(encoding="utf-8")

        self.assertIn("if _ensure_mcd_service_kill_mode():", daemon_source)

    def test_existing_service_unit_is_migrated_to_control_group(self) -> None:
        with TemporaryDirectory() as tmp:
            unit = Path(tmp) / "mcd.service"
            unit.write_text(
                "[Service]\nExecStart=/opt/mcd/venv/bin/python -m mcd_agent\nKillMode=control-group\n",
                encoding="utf-8",
            )
            with patch.object(
                self_update.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ) as run:
                changed = self_update._ensure_mcd_service_kill_mode(unit)
                content = unit.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertIn("KillMode=process", content)
        run.assert_called_once_with(["systemctl", "daemon-reload"], capture_output=True, text=True)

    def test_current_service_unit_does_not_reload_systemd(self) -> None:
        with TemporaryDirectory() as tmp:
            unit = Path(tmp) / "mcd.service"
            unit.write_text("[Service]\nKillMode=process\n", encoding="utf-8")
            with patch.object(self_update.subprocess, "run") as run:
                changed = self_update._ensure_mcd_service_kill_mode(unit)

        self.assertFalse(changed)
        run.assert_not_called()

    def test_service_dropin_cannot_override_control_group_with_process(self) -> None:
        with TemporaryDirectory() as tmp:
            unit = Path(tmp) / "mcd.service"
            unit.write_text("[Service]\nKillMode=process\n", encoding="utf-8")
            dropin_dir = Path(tmp) / "mcd.service.d"
            dropin_dir.mkdir()
            override = dropin_dir / "override.conf"
            override.write_text("[Service]\nKillMode=control-group\nTimeoutStopSec=15\n", encoding="utf-8")
            with patch.object(
                self_update.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ) as run:
                changed = self_update._ensure_mcd_service_kill_mode(unit)
                override_content = override.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertIn("KillMode=process", override_content)
        run.assert_called_once_with(["systemctl", "daemon-reload"], capture_output=True, text=True)

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

    def test_package_sync_clears_editable_source_shadow_and_isolates_venv_probe(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_dir = root / "mcd"
            source_dir = install_dir / "src"
            venv_python = install_dir / "venv" / "bin" / "python"
            site_dir = install_dir / "venv" / "lib" / "python3.12" / "site-packages"
            source_dir.mkdir(parents=True)
            (source_dir / "mcd_agent").mkdir()
            (source_dir / "pyproject.toml").write_text(
                "[project]\nname='mcd-agent'\nversion='1.5.78'\n",
                encoding="utf-8",
            )
            (source_dir / "mcd_agent" / "__init__.py").write_text(
                '__version__ = "1.5.78"\n',
                encoding="utf-8",
            )
            venv_python.parent.mkdir(parents=True)
            venv_python.touch()
            site_dir.mkdir(parents=True)

            (site_dir / "__editable__.mcd_agent-1.5.77.pth").write_text(
                "import __editable___mcd_agent_1_5_77_finder; __editable___mcd_agent_1_5_77_finder.install()\n",
                encoding="utf-8",
            )
            (site_dir / "__editable___mcd_agent_1_5_77_finder.py").write_text(
                "# stale finder\n",
                encoding="utf-8",
            )
            (site_dir / "mcd_agent.egg-link").write_text(str(source_dir) + "\n", encoding="utf-8")
            (site_dir / "easy-install.pth").write_text(
                f"{source_dir}\n/other/package\n",
                encoding="utf-8",
            )

            calls: list[tuple[list[str], dict[str, object]]] = []

            def run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
                calls.append((cmd, kwargs))
                code = cmd[-1] if "-c" in cmd else ""
                if "site.getsitepackages" in code:
                    return SimpleNamespace(returncode=0, stdout=f"{site_dir}\n", stderr="")
                if "import importlib.metadata" in code:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"1.5.78\n1.5.78\n{site_dir / 'mcd_agent' / '__init__.py'}\n",
                        stderr="",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.dict(
                self_update.os.environ,
                {"PYTHONPATH": str(source_dir), "PYTHONHOME": str(root / "wrong-python")},
                clear=False,
            ), patch.object(self_update, "_ensure_venv_pip"), patch.object(
                self_update.subprocess, "run", side_effect=run
            ):
                self_update._install_agent_package_for_source(install_dir, source_dir)

            self.assertFalse((site_dir / "__editable__.mcd_agent-1.5.77.pth").exists())
            self.assertFalse((site_dir / "__editable___mcd_agent_1_5_77_finder.py").exists())
            self.assertFalse((site_dir / "mcd_agent.egg-link").exists())
            self.assertEqual(
                (site_dir / "easy-install.pth").read_text(encoding="utf-8"),
                "/other/package\n",
            )
            self.assertTrue((site_dir / "mcd_agent" / "__init__.py").exists())
            for _cmd, kwargs in calls:
                env = kwargs["env"]
                self.assertNotIn("PYTHONPATH", env)
                self.assertNotIn("PYTHONHOME", env)

    def test_apply_update_rolls_back_and_resynchronizes_package_after_path_mismatch(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _cfg(tmp)
            cfg.mcd_install_dir = str(root / "mcd")
            cfg.config_file_path = ""
            cfg.mcd_update_cleanup_enabled = False
            install_dir = Path(cfg.mcd_install_dir)
            src_dir = install_dir / "src"
            (src_dir / "mcd_agent").mkdir(parents=True)
            (src_dir / "old-marker").write_text("old\n", encoding="utf-8")
            archive = root / "mcd-agent-1.5.78.tar.gz"
            archive.touch()
            calls: list[Path] = []
            releases: list[dict[str, object]] = []

            def install_package(_install_dir: Path, source_dir: Path) -> None:
                calls.append(source_dir)
                if len(calls) == 1:
                    raise RuntimeError(
                        "installed package path mismatch expected=/opt/mcd/venv/lib/python3.12/site-packages/"
                        "mcd_agent/__init__.py imported=/opt/mcd/src/mcd_agent/__init__.py"
                    )

            def extract_archive(_archive: Path, destination: Path) -> None:
                (destination / "mcd_agent").mkdir(parents=True)
                (destination / "mcd_agent" / "__init__.py").write_text(
                    '__version__ = "1.5.78"\n',
                    encoding="utf-8",
                )

            old_installed = self_update.installed_agent_version
            old_release = self_update.release_session
            try:
                self_update.installed_agent_version = lambda: "1.5.75"
                self_update.release_session = lambda _cfg, _session_id, **kwargs: releases.append(kwargs)
                with patch.object(self_update, "capture_profile_authority", return_value={}), patch.object(
                    self_update, "restore_profile_authority"
                ), patch.object(self_update, "_update_archive_path", return_value=archive), patch.object(
                    self_update, "_ensure_update_archive", return_value=archive
                ), patch.object(
                    self_update, "_extract_archive_to_dir", side_effect=extract_archive
                ), patch.object(
                    self_update, "_install_requirements_for_staged_source"
                ), patch.object(
                    self_update, "_pre_switch_smoke_check"
                ), patch.object(
                    self_update, "_install_agent_package_for_source", side_effect=install_package
                ):
                    ok, msg = self_update.apply_update(
                        cfg,
                        {
                            "status": "update",
                            "target": "1.5.78",
                            "package_url": "https://mcc.invalid/mcd-agent-1.5.78.tar.gz",
                            "session_id": "sess-path-mismatch",
                        },
                    )
            finally:
                self_update.installed_agent_version = old_installed
                self_update.release_session = old_release

            self.assertFalse(ok)
            self.assertIn("installed package path mismatch", msg)
            self.assertNotIn("rollback failed", msg)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1], src_dir)
            self.assertEqual((src_dir / "old-marker").read_text(encoding="utf-8"), "old\n")
            self.assertEqual(releases[0]["result_status"], "failed")

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
