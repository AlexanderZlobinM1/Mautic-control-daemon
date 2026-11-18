from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import cli
from mcd_agent import runtime_integrity as integrity


def _fixture(tmp_path: Path) -> tuple[Path, str, str, str]:
    install = tmp_path / "mcd"
    runtime = install / "runtime" / "mcd_agent"
    runtime.mkdir(parents=True)
    daemon = runtime / "daemon.py"
    daemon.write_text("# candidate daemon\n", encoding="utf-8")
    wheel = install / "runtime" / "wheel" / "mcd_agent-1.6.107-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"candidate wheel")
    daemon_sha = hashlib.sha256(daemon.read_bytes()).hexdigest()
    wheel_sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest = {
        "schema": "mcd-runtime-package-v1",
        "version": "1.6.107",
        "source_commit": "candidate-commit",
        "wheel": "wheel/mcd_agent-1.6.107-py3-none-any.whl",
        "payload_sha256": {
            "mcd_agent/daemon.py": daemon_sha,
            "wheel/mcd_agent-1.6.107-py3-none-any.whl": wheel_sha,
        },
    }
    (install / "runtime" / "mcd-runtime-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return install, daemon_sha, wheel_sha, str(install / "venv" / "bin" / "python")


def _run_check(tmp_path: Path, *, current: dict, systemd: dict, wrapper: dict, exec_start: str | None = None):
    install, daemon_sha, _wheel_sha, interpreter = _fixture(tmp_path)
    exec_start = exec_start or f"path={interpreter}; argv[]={interpreter} -m mcd_agent run"

    def prop(_service: str, name: str):
        return {
            "ExecStart": exec_start,
            "ActiveState": "active",
            "MainPID": "1234",
        }[name], ""

    with (
        patch.object(integrity, "_module_snapshot", return_value=current),
        patch.object(integrity, "_systemd_property", side_effect=prop),
        patch.object(integrity, "_probe_interpreter", return_value=systemd),
        patch.object(integrity, "_wrapper_path", return_value=Path("/opt/mcd/venv/bin/mcd-cli")),
        patch.object(integrity, "_wrapper_probe", return_value=wrapper),
    ):
        return integrity.check_runtime_integrity(
            install_dir=install,
            expected_version="1.6.107",
            expected_source_commit="candidate-commit",
            expected_daemon_sha256=daemon_sha,
            strict=True,
        )


def test_stale_package_metadata_is_not_hidden_by_wrapper_version(tmp_path: Path) -> None:
    current = {
        "running_version": "1.6.107",
        "package_version": "1.6.105",
        "daemon_sha256": "a" * 64,
        "module_path": "/opt/mcd/runtime/mcd_agent/__init__.py",
    }
    systemd = {**current, "python": "/opt/mcd/venv/bin/python"}
    result = _run_check(
        tmp_path,
        current=current,
        systemd=systemd,
        wrapper={"status": "ok", "version": "1.6.107"},
    )

    codes = {item["code"] for item in result["errors"]}
    assert result["status"] == "failed"
    assert "runtime_current_package_metadata_mismatch" in codes
    assert "runtime_systemd_package_metadata_mismatch" in codes
    assert "runtime_systemd_daemon_hash_mismatch" in codes


def test_wrong_systemd_execstart_venv_fails_even_when_wrapper_is_new(tmp_path: Path) -> None:
    daemon_sha = hashlib.sha256(b"# candidate daemon\n").hexdigest()
    exact = {
        "running_version": "1.6.107",
        "package_version": "1.6.107",
        "daemon_sha256": daemon_sha,
        "module_path": "/opt/mcd/venv/lib/python3.12/site-packages/mcd_agent/__init__.py",
    }
    result = _run_check(
        tmp_path,
        current=exact,
        systemd=exact,
        wrapper={"status": "ok", "version": "1.6.107"},
        exec_start="path=/opt/other-venv/bin/python; argv[]=/opt/other-venv/bin/python -m mcd_agent run",
    )

    assert result["status"] == "failed"
    assert any(item["code"] == "runtime_systemd_interpreter_mismatch" for item in result["errors"])


def test_stale_daemon_hash_fails_despite_healthy_version_strings(tmp_path: Path) -> None:
    stale = {
        "running_version": "1.6.107",
        "package_version": "1.6.107",
        "daemon_sha256": "b" * 64,
        "module_path": "/opt/mcd/venv/lib/python3.12/site-packages/mcd_agent/__init__.py",
    }
    result = _run_check(
        tmp_path,
        current=stale,
        systemd=stale,
        wrapper={"status": "ok", "version": "1.6.107"},
    )

    codes = {item["code"] for item in result["errors"]}
    assert result["status"] == "failed"
    assert "runtime_current_daemon_hash_mismatch" in codes
    assert "runtime_systemd_daemon_hash_mismatch" in codes


def test_exact_runtime_parity_is_accepted(tmp_path: Path) -> None:
    install, daemon_sha, _wheel_sha, interpreter = _fixture(tmp_path)
    exact = {
        "running_version": "1.6.107",
        "package_version": "1.6.107",
        "daemon_sha256": daemon_sha,
        "module_path": "/opt/mcd/venv/lib/python3.12/site-packages/mcd_agent/__init__.py",
    }
    with (
        patch.object(integrity, "_module_snapshot", return_value=exact),
        patch.object(
            integrity,
            "_systemd_property",
            side_effect=lambda _service, name: ({
                "ExecStart": f"path={interpreter}; argv[]={interpreter} -m mcd_agent run",
                "ActiveState": "active",
                "MainPID": "1234",
            }[name], ""),
        ),
        patch.object(integrity, "_probe_interpreter", return_value={**exact, "python": interpreter}),
        patch.object(integrity, "_wrapper_path", return_value=Path("/opt/mcd/venv/bin/mcd-cli")),
        patch.object(integrity, "_wrapper_probe", return_value={"status": "ok", "version": "1.6.107"}),
    ):
        result = integrity.check_runtime_integrity(
            install_dir=install,
            expected_version="1.6.107",
            expected_source_commit="candidate-commit",
            expected_daemon_sha256=daemon_sha,
            strict=True,
        )

    assert result["status"] == "ok"
    assert result["errors"] == []


def test_health_returns_nonzero_on_runtime_parity_failure(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setattr("sys.argv", ["mcd-cli", "health", "--json"])
    with (
        patch.object(cli, "load_config", return_value=SimpleNamespace(mcd_install_dir=str(tmp_path))),
        patch.object(cli, "agent_version_payload", return_value={"agent_version": "1.6.107"}),
        patch.object(cli, "check_runtime_integrity", return_value={
            "status": "failed",
            "errors": [{"code": "runtime_systemd_daemon_hash_mismatch"}],
        }),
    ):
        assert cli.main() == 1
    assert "runtime_systemd_daemon_hash_mismatch" in capsys.readouterr().out
