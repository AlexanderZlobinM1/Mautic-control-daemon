#!/usr/bin/env python3
"""Disposable proof for the legacy-source to runtime-only bridge.

The helper creates a temporary MCD-shaped install, starts a real long-lived
worker, persists queue/running state in TaskStore, runs the component bridge
against an exact runtime archive, and verifies that the worker, state DB, and
post-switch site-packages import survive.  It never touches a host service or
Mautic installation.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcd_agent.daemon import RunningTask, TaskStore


def _load_bridge():
    path = Path(__file__).with_name("mcd_runtime_bootstrap.py")
    spec = importlib.util.spec_from_file_location("mcd_runtime_bootstrap", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load bridge: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "command failed").strip()
        raise RuntimeError(f"{' '.join(command)}: {detail}")
    return proc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_version(path: Path) -> str:
    with tarfile.open(path, "r:gz") as archive:
        try:
            member = archive.extractfile("mcd-runtime-manifest.json")
        except KeyError as exc:
            raise RuntimeError("runtime archive is missing mcd-runtime-manifest.json") from exc
        if member is None:
            raise RuntimeError("runtime archive manifest is unreadable")
        raw = json.loads(member.read().decode("utf-8"))
    version = str(raw.get("version") or "").strip() if isinstance(raw, dict) else ""
    if not version:
        raise RuntimeError("runtime archive manifest has no version")
    return version


def _archive_metadata(path: Path) -> dict[str, str]:
    with tarfile.open(path, "r:gz") as archive:
        member = archive.extractfile("mcd-runtime-manifest.json")
        if member is None:
            raise RuntimeError("runtime archive manifest is unreadable")
        raw = member.read()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("runtime archive manifest is not an object")
    return {
        "version": str(payload.get("version") or "").strip(),
        "source_commit": str(payload.get("source_commit") or "").strip(),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _cfg(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        state_db_path=str(root / "install" / "var" / "state.db"),
        config_file_path="",
        mcd_install_dir=str(root / "install"),
        backup_enabled=False,
        mcc_host_name="qa-runtime-bridge",
        mcd_update_policy="test",
        mcd_update_channel="test",
        mcd_auto_update_enabled=False,
    )


def _write_legacy_source(install: Path, *, version: str = "1.5.131") -> None:
    source = install / "src"
    package = source / "mcd_agent"
    package.mkdir(parents=True)
    # This is the exact old-agent failure layout: the runtime-only archive
    # cannot satisfy the legacy updater's pyproject-based source install.
    (package / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable, help="Python used to create the disposable venv/worker")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-target", default="", help="Expected manifest version; defaults to the archive manifest")
    parser.add_argument("--expected-archive-sha256", default="", help="Expected immutable archive SHA-256")
    args = parser.parse_args(argv)
    archive = args.archive.resolve()
    if not archive.is_file():
        raise RuntimeError(f"runtime archive not found: {archive}")
    archive_sha256 = _sha256(archive)
    expected_sha256 = str(args.expected_archive_sha256 or "").strip()
    if expected_sha256 and archive_sha256 != expected_sha256:
        raise RuntimeError(f"runtime archive SHA mismatch: expected {expected_sha256}, got {archive_sha256}")
    expected_target = str(args.expected_target or "").strip() or _archive_version(archive)
    archive_metadata = _archive_metadata(archive)
    with tarfile.open(archive, "r:*") as archive_handle:
        archive_names = archive_handle.getnames()
    archive_has_no_pyproject = not any(Path(name).name == "pyproject.toml" for name in archive_names)
    if not archive_has_no_pyproject:
        raise RuntimeError("runtime-only archive unexpectedly contains pyproject.toml")

    bridge = _load_bridge()
    worker: subprocess.Popen[str] | None = None
    with tempfile.TemporaryDirectory(prefix="mcd-runtime-bridge-qa-") as tmp:
        root = Path(tmp)
        install = root / "install"
        (install / "var").mkdir(parents=True)
        (install / "etc").mkdir()
        _write_legacy_source(install)
        _run([args.python, "-m", "venv", str(install / "venv")])

        cfg = _cfg(root)
        queue_state = {
            "ordinary:/var/www/qa": {
                "queue": [101, 102],
                "follow_up": [103],
                "claims": [101],
                "running": [101],
                "retry": {"102": 1},
                "monitor": {"cycle": "bridge-qa-1", "status": "running"},
            }
        }
        store = TaskStore(cfg.state_db_path, cfg)
        for key, payload in queue_state.items():
            store.put_runtime_sync(key, payload)
        worker = subprocess.Popen([args.python, "-c", "import time; time.sleep(180)"])
        task = RunningTask(
            row_id=0,
            root="/var/www/qa",
            task_key="/var/www/qa|segment|101",
            task_type="segment",
            entity_id=101,
            command_str="python -c import time; time.sleep(180)",
            timeout_sec=3600,
            attempts=1,
            started_at=time.time(),
            pid=worker.pid,
            manual_request_id=None,
        )
        task.row_id = store.add_running(task)
        store.close()

        args_ns = SimpleNamespace(
            install_dir=str(install),
            archive=str(archive),
            config=str(install / "etc" / "mcd.toml"),
            expected_archive_sha256=archive_sha256,
            expected_manifest_sha256="",
            expected_target=expected_target,
            service_name="mcd",
            systemctl="systemctl",
            systemd_dir=str(root / "systemd"),
            health_timeout_sec=10,
            no_systemd=True,
            sandbox=True,
        )
        receipt = bridge.bridge(args_ns)

        reopened = TaskStore(cfg.state_db_path, cfg)
        preserved = all(reopened.get_runtime_sync(key) == payload for key, payload in queue_state.items())
        rows = reopened.running_rows()
        reopened.close()
        if not preserved:
            raise RuntimeError("TaskStore queue/claim/running/retry/monitor state changed")
        if worker.poll() is not None:
            raise RuntimeError("ordinary worker did not survive bridge")
        if not (install / "src" / "mcd-runtime-manifest.json").is_file():
            raise RuntimeError("runtime source switch did not install the detached manifest")
        if not (install / "var" / "runtime-bootstrap-receipt.json").is_file():
            raise RuntimeError("bridge receipt is missing")
        if rows and not any(int(row.get("pid") or 0) == worker.pid for row in rows if isinstance(row, dict)):
            raise RuntimeError("running TaskStore row was not preserved")

        old_source_path = Path(str(receipt["before"]["source_path"]))
        legacy_source_without_pyproject = not (old_source_path / "pyproject.toml").exists()
        if not legacy_source_without_pyproject:
            raise RuntimeError("success bridge QA did not use the no-pyproject legacy layout")

        # Inject a post-switch failure after the package/source mutation and
        # verify the exact rollback contract for a legacy 1.5.133 layout.
        failure_root = root / "failure"
        failure_install = failure_root / "install"
        (failure_install / "var").mkdir(parents=True)
        (failure_install / "etc").mkdir(parents=True)
        _write_legacy_source(failure_install, version="1.5.133")
        _run([args.python, "-m", "venv", str(failure_install / "venv")])
        failure_cfg = _cfg(failure_root)
        failure_state = {
            "last_status": "deferred_backup_lock",
            "last_target": expected_target,
            "operator_note": "preserve-me",
        }
        failure_state_path = failure_install / "var" / "mcd-self-update.json"
        failure_state_path.write_text(json.dumps(failure_state, sort_keys=True) + "\n", encoding="utf-8")
        failure_queue = {
            "ordinary:/var/www/legacy-133": {
                "queue": [201, 202],
                "follow_up": [203],
                "claims": [201],
                "running": [201],
                "retry": {"202": 2},
                "monitor": {"cycle": "legacy-133", "status": "running"},
            }
        }
        failure_store = TaskStore(failure_cfg.state_db_path, failure_cfg)
        for key, payload in failure_queue.items():
            failure_store.put_runtime_sync(key, payload)
        failure_worker = subprocess.Popen([args.python, "-c", "import time; time.sleep(180)"])
        failure_task = RunningTask(
            row_id=0,
            root="/var/www/legacy-133",
            task_key="/var/www/legacy-133|segment|201",
            task_type="segment",
            entity_id=201,
            command_str="python -c import time; time.sleep(180)",
            timeout_sec=3600,
            attempts=1,
            started_at=time.time(),
            pid=failure_worker.pid,
            manual_request_id=None,
        )
        failure_task.row_id = failure_store.add_running(failure_task)
        failure_store.close()
        failure_args = SimpleNamespace(**vars(args_ns))
        failure_args.install_dir = str(failure_install)
        failure_args.config = str(failure_install / "etc" / "mcd.toml")
        failure_error = ""
        try:
            with patch.object(bridge, "_install_runtime_probe", side_effect=RuntimeError("qa injected post-switch failure")):
                bridge.bridge(failure_args)
        except Exception as exc:  # noqa: BLE001 - the injected failure is the proof
            failure_error = str(exc)
        finally:
            failure_worker_alive = failure_worker.poll() is None
            failure_worker.terminate()
            failure_worker.wait(timeout=10)
        if "qa injected post-switch failure" not in failure_error:
            raise RuntimeError(f"failure rollback probe did not return injected error: {failure_error}")
        failure_reopened = TaskStore(failure_cfg.state_db_path, failure_cfg)
        failure_preserved = all(
            failure_reopened.get_runtime_sync(key) == payload for key, payload in failure_queue.items()
        )
        failure_rows = failure_reopened.running_rows()
        failure_reopened.close()
        failure_source = failure_install / "src"
        failure_site = bridge._site_dir(bridge._venv_python(failure_install))
        failure_agent_entries = sorted(
            entry.name for entry in failure_site.iterdir() if bridge._is_agent_runtime_entry(entry)
        )
        failure_pause = failure_install / "var" / "scheduler.pause"
        failure_updates = failure_install / "var" / "updates"
        failure_rollback = {
            "source_restored": (failure_source / "mcd_agent" / "__init__.py").is_file()
            and not (failure_source / "pyproject.toml").exists()
            and not (failure_source / "mcd-runtime-manifest.json").exists(),
            "owned_pause_removed": not failure_pause.exists(),
            "state_restored": failure_state_path.read_text(encoding="utf-8")
            == json.dumps(failure_state, sort_keys=True) + "\n",
            "queue_state_preserved": failure_preserved,
            "worker_pid_preserved": failure_worker_alive
            and any(
                int(row.get("pid") or 0) == failure_worker.pid
                for row in failure_rows
                if isinstance(row, dict)
            ),
            "managed_venv_entries_restored": failure_agent_entries == [],
            "temporary_snapshots_cleaned": not any(
                path.name.startswith("legacy-runtime-") for path in failure_updates.iterdir()
            ),
            "error": failure_error,
            "source_path": str(failure_source),
            "state_path": str(failure_state_path),
            "pause_path": str(failure_pause),
        }
        if not all(
            bool(failure_rollback[key])
            for key in (
                "source_restored",
                "owned_pause_removed",
                "state_restored",
                "queue_state_preserved",
                "worker_pid_preserved",
                "managed_venv_entries_restored",
                "temporary_snapshots_cleaned",
            )
        ):
            raise RuntimeError(f"legacy rollback contract failed: {failure_rollback}")

        regression_env = dict(os.environ)
        regression_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        regression = subprocess.run(
            [
                args.python,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(Path(__file__).resolve().parents[1] / "tests"),
                "-p",
                "test_mode_cron_wrappers.py",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=regression_env,
            capture_output=True,
            text=True,
        )
        if regression.returncode != 0:
            detail = (regression.stderr or regression.stdout or "cron/version regression failed").strip()
            raise RuntimeError(f"cron/version regression failed: {detail}")

        result = {
            "schema": "mcd-runtime-bridge-qa-v1",
            "status": "PASS",
            "archive_sha256": archive_sha256,
            "source_commit": archive_metadata["source_commit"],
            "version": archive_metadata["version"],
            "manifest_sha256": archive_metadata["manifest_sha256"],
            "required_checks": {
                "cron_version_regression": "passed",
                "cron_version_regression_test": "tests/test_mode_cron_wrappers.py",
            },
            "bridge_receipt": receipt,
            "taskstore_preserved": preserved,
            "worker_pid_preserved": worker.poll() is None,
            "runtime_manifest_present": True,
            "legacy_layout": {
                "versions": ["1.5.131", "1.5.133"],
                "source_without_pyproject": legacy_source_without_pyproject,
                "runtime_archive_without_pyproject": archive_has_no_pyproject,
                "normal_apply_target": expected_target,
            },
            "failure_rollback": failure_rollback,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        print(args.output)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        worker.terminate()
        worker.wait(timeout=10)
        worker = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
