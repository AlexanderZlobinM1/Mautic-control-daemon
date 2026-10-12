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
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace

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


def _write_legacy_source(install: Path) -> None:
    source = install / "src"
    package = source / "mcd_agent"
    package.mkdir(parents=True)
    (source / "pyproject.toml").write_text(
        "[build-system]\nrequires=['setuptools>=61']\nbuild-backend='setuptools.build_meta'\n"
        "[project]\nname='mcd-agent'\nversion='1.5.127'\n",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text('__version__ = "1.5.127"\n', encoding="utf-8")


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

        result = {
            "schema": "mcd-runtime-bridge-qa-v1",
            "status": "PASS",
            "archive_sha256": archive_sha256,
            "bridge_receipt": receipt,
            "taskstore_preserved": preserved,
            "worker_pid_preserved": worker.poll() is None,
            "runtime_manifest_present": True,
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
