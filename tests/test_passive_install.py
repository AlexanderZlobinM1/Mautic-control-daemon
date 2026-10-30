"""Admission checks must fail before network or host writes."""
import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.fixture
def installer():
    path = Path(__file__).parents[1] / "tools/mcd_passive_install.py"
    spec = importlib.util.spec_from_file_location("passive_install_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("url", ["http://portal.example.com", "https://user:pass@portal.example.com", "https://portal.example.com/#fragment"])
def test_https_rejects_unsafe_transport(installer, url):
    with pytest.raises(ValueError, match="bootstrap_https_required"):
        installer.https(url)


def test_redirect_never_forwards_key(installer):
    with pytest.raises(RuntimeError, match="bootstrap_redirect_refused"):
        installer.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://other.example.com")


@pytest.mark.parametrize("identity", ["host@server", "../escape", "localhost", "example.com/path"])
def test_instance_identity_rejected_before_network_or_host_writes(installer, monkeypatch, identity):
    monkeypatch.setattr(sys, "argv", ["installer", "--portal-api-base", "https://portal.example.com/api/v1/commercial", "--archive-uri", "https://portal.example.com/runtime", "--archive-sha256", "a" * 64, "--manifest-sha256", "b" * 64, "--runtime-version", "1.6.25", "--request-id", "test", "--instance-identity", identity, "--bridge", "/nonexistent", "--bridge-sha256", "c" * 64])
    with pytest.raises(ValueError, match="bootstrap_instance_identity_invalid"):
        installer.main()


def test_subprocess_failure_does_not_expose_key(installer, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(installer.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="sensitive-key", stderr="sensitive-key"))
    with pytest.raises(RuntimeError, match="^bootstrap_command_failed:python$"):
        installer.run("/venv/bin/python", data="sensitive-key")


def test_passive_config_cannot_be_overridden_by_example_includes(installer, tmp_path):
    from mcd_agent.config import _load_toml_with_includes
    config = tmp_path / "mcd.toml"
    config.write_text(installer.PASSIVE_CONFIG)
    parsed = _load_toml_with_includes(str(config))
    assert parsed["profile"]["name"] == "passive"
    assert parsed["mcc"] == {"url": "", "token": "", "push_enabled": False}
    assert "include" not in parsed


@pytest.mark.parametrize("mismatch", [None, "request_id", "local_identity", "archive_sha256", "unit", "missing_database"])
def test_completed_replay_requires_exact_durable_install(installer, tmp_path, monkeypatch, mismatch):
    import json
    from types import SimpleNamespace
    root = tmp_path / "mcd"
    (root / "var").mkdir(parents=True)
    (root / "etc").mkdir()
    (root / "var/mcd-state.db").touch()
    (root / "etc/mcd.toml").write_text(installer.PASSIVE_CONFIG)
    unit = tmp_path / "mcd.service"
    unit.write_text(installer.PASSIVE_UNIT)
    args = SimpleNamespace(runtime_version="1.6.25", archive_sha256="a" * 64, manifest_sha256="b" * 64, bridge_sha256="c" * 64, request_id="fixture", portal_api_base="https://portal.example.com/api/v1/commercial")
    receipt = {"schema": "mcd-passive-install-receipt-v1", "version": args.runtime_version, "archive_sha256": args.archive_sha256, "manifest_sha256": args.manifest_sha256, "bridge_sha256": args.bridge_sha256, "request_id": "fixture", "local_identity": "site.example.invalid", "activation_state": "passive", "service_started": False, "agent_id": "agent-1", "installation_id": "installation-1"}
    import hashlib
    receipt["host_fingerprint"] = "sha256:" + hashlib.sha256(installer.socket.gethostname().encode()).hexdigest()
    if mismatch == "unit":
        unit.write_text("wrong")
    elif mismatch == "missing_database":
        (root / "var/mcd-state.db").unlink()
    elif mismatch:
        receipt[mismatch] = "different"
    (root / "var/passive-install-receipt.json").write_text(json.dumps(receipt))
    calls = []
    def run(*argv, data=None):
        calls.append(argv)
        return ("disabled\n" if "--property=UnitFileState" in argv else "inactive\n") if argv[0] == "systemctl" else ""
    monkeypatch.setattr(installer, "run", run)
    if mismatch:
        with pytest.raises(RuntimeError):
            installer.completed_replay(root, unit, args, "fixture-key", "site.example.invalid")
        assert calls == []
    else:
        result = installer.completed_replay(root, unit, args, "fixture-key", "site.example.invalid")
        assert result["idempotent_replay"] is True
        assert all("fixture-key" not in argv for argv in calls)
        assert len(calls) == 3


@pytest.mark.parametrize("active,enabled", [("active", "disabled"), ("inactive", "enabled")])
def test_passive_receipt_requires_inactive_and_disabled_service(installer, monkeypatch, active, enabled):
    monkeypatch.setattr(installer, "run", lambda *argv: enabled if "--property=UnitFileState" in argv else active)
    with pytest.raises(RuntimeError, match="bootstrap_service_not_"):
        installer.require_passive_service()
