#!/usr/bin/env python3
"""Disposable runtime-only migration, rollback, and adoption proof.

This is a release-gate helper.  It creates only a temporary venv/installation,
never touches a host or Mautic instance, and emits a JSON receipt suitable for
the immutable candidate handoff.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcd_agent import __version__, self_update
from mcd_agent.daemon import RunningTask, TaskStore, _reconcile_running_state


def _run(cmd: list[str], *, cwd: Path | None = None) -> None:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "command failed").strip()
        raise RuntimeError(f"{' '.join(cmd)}: {detail}")


def _build(source: Path, output: Path, commit: str) -> None:
    _run(
        [
            sys.executable,
            str(source / "tools" / "build-runtime-archive.py"),
            "--source",
            str(source),
            "--output",
            str(output),
            "--source-commit",
            commit,
        ],
        cwd=source,
    )


def _extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    _run(["tar", "-xzf", str(archive), "-C", str(destination)])


def _cfg(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        state_db_path=str(root / "state.db"),
        config_file_path="",
        mcd_install_dir=str(root / "install"),
        backup_enabled=False,
        mcd_update_cleanup_enabled=False,
        mcd_update_cleanup_interval_sec=86400,
        mcd_update_keep_archives=3,
        mcd_update_keep_preupdate_backups=3,
        mcd_update_artifacts_max_age_days=30,
        mcc_host_name="qa-runtime",
        mcd_update_policy="test",
        mcd_update_channel="test",
        mcd_auto_update_enabled=True,
    )


def main() -> int:
    source = Path(__file__).resolve().parents[1]
    target_version = str(__version__)
    target_parts = target_version.split(".")
    if len(target_parts) != 3:
        raise RuntimeError(f"unexpected agent version: {target_version}")
    previous_version = ".".join([target_parts[0], target_parts[1], str(max(0, int(target_parts[2]) - 1))])
    with tempfile.TemporaryDirectory(prefix="mcd-runtime-migration-qa-") as tmp:
        root = Path(tmp)
        install = root / "install"
        venv = install / "venv"
        _run([sys.executable, "-m", "venv", str(venv)])

        archive = root / f"runtime-{target_version}.tar.gz"
        _build(source, archive, f"runtime-{target_version}-source")
        current_src = install / "src"
        _extract(archive, current_src)
        cfg = _cfg(root)
        self_update._install_requirements_for_staged_source(install, current_src)
        self_update._pre_switch_smoke_check(install, current_src)
        self_update._install_agent_package_for_source(install, current_src)

        queue_state = {
            "segment_cycle:/var/www/qa": {
                "cycle_id": "qa-cycle-1",
                "items": [101, 102],
                "follow_up": [103],
                "claimed": [101],
                "running": [101],
                "done": [],
                "errors": [],
                "status": "active",
            },
            "import_queue:/var/www/qa": {
                "queue": "import",
                "queued": [1, 2],
                "claimed": [1],
                "running": [1],
                "done": [],
            },
        }
        store = TaskStore(cfg.state_db_path, cfg)
        for key, payload in queue_state.items():
            store.put_runtime_sync(key, payload)
        task = RunningTask(
            row_id=0,
            root="/var/www/qa",
            task_key="/var/www/qa|segment|101",
            task_type="segment",
            entity_id=101,
            command_str="php /var/www/qa/bin/console mautic:segments:update -i 101",
            timeout_sec=3600,
            attempts=1,
            started_at=1234.0,
            pid=4321,
            manual_request_id=None,
        )
        task.row_id = store.add_running(task)
        store.close()

        old_installed = self_update.installed_agent_version
        old_archive = self_update._ensure_update_archive
        old_release = self_update.release_session
        old_restart = self_update._restart_service_async
        try:
            self_update.installed_agent_version = lambda: previous_version
            self_update._ensure_update_archive = lambda _cfg, _plan: archive
            self_update.release_session = lambda *_args, **_kwargs: None
            self_update._restart_service_async = lambda: None
            ok, migration_message = self_update.apply_update(
                cfg,
                {
                    "status": "update",
                    "target": target_version,
                    "package_url": f"https://qa.invalid/mcd-agent-runtime-{target_version}.tar.gz",
                },
            )
        finally:
            self_update.installed_agent_version = old_installed
            self_update._ensure_update_archive = old_archive
            self_update.release_session = old_release
            self_update._restart_service_async = old_restart
        if not ok:
            raise RuntimeError(f"runtime migration failed: {migration_message}")
        migration_receipt = json.loads((install / "var" / "runtime-package-receipt.json").read_text())

        old_install = self_update.installed_agent_version
        old_archive = self_update._ensure_update_archive
        old_release = self_update.release_session
        old_restart = self_update._restart_service_async
        old_runtime_install = self_update._install_agent_package_from_runtime_wheel
        calls = {"count": 0}

        def fail_once(*args: object, **kwargs: object) -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("qa injected runtime install failure")
            old_runtime_install(*args, **kwargs)

        try:
            self_update.installed_agent_version = lambda: previous_version
            self_update._ensure_update_archive = lambda _cfg, _plan: archive
            self_update.release_session = lambda *_args, **_kwargs: None
            self_update._restart_service_async = lambda: None
            self_update._install_agent_package_from_runtime_wheel = fail_once
            rollback_ok, rollback_message = self_update.apply_update(
                cfg,
                {
                    "status": "update",
                    "target": target_version,
                    "package_url": f"https://qa.invalid/mcd-agent-runtime-{target_version}.tar.gz",
                },
            )
        finally:
            self_update.installed_agent_version = old_install
            self_update._ensure_update_archive = old_archive
            self_update.release_session = old_release
            self_update._restart_service_async = old_restart
            self_update._install_agent_package_from_runtime_wheel = old_runtime_install
        if rollback_ok or "qa injected runtime install failure" not in rollback_message:
            raise RuntimeError(f"rollback proof did not fail-and-restore: {rollback_message}")
        rollback_receipt = json.loads((install / "var" / "runtime-package-receipt.json").read_text())
        if rollback_receipt["package_version"] != target_version or not (install / "src" / "mcd-runtime-manifest.json").is_file():
            raise RuntimeError("rollback did not restore the previous runtime payload")

        reopened = TaskStore(cfg.state_db_path, cfg)
        for key, payload in queue_state.items():
            if reopened.get_runtime_sync(key) != payload:
                raise RuntimeError(f"queue state changed during migration: {key}")
        running: dict[str, object] = {}
        with patch("mcd_agent.daemon._is_pid_alive", return_value=True), patch(
            "mcd_agent.daemon._pid_matches_task_command", return_value=True
        ):
            adoption = _reconcile_running_state(store=reopened, running=running, popens={})
        reopened.close()
        if adoption.get("adopted") != 1 or "/var/www/qa|segment|101" not in running:
            raise RuntimeError(f"TaskStore adoption failed: {adoption}")

        receipt = {
            "schema": "mcd-runtime-migration-qa-v1",
            "runtime_only": True,
            "migration": {"ok": True, "message": migration_message, "receipt": migration_receipt},
            "rollback": {
                "ok": True,
                "message": rollback_message,
                "receipt": rollback_receipt,
                "injected_failure_calls": calls["count"],
            },
            "adoption": {"ok": True, **adoption},
            "queues_preserved": True,
        }
        output = Path(os.environ.get("MCD_RUNTIME_QA_RECEIPT", str(root / "receipt.json")))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(receipt, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        print(output)
        print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
