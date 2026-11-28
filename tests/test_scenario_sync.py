from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from mcd_agent import scenario_sync


def _payload(package: bytes) -> dict[str, object]:
    row = {
        "id": "mautic-upgrade",
        "version": "7.2.3",
        "entrypoint": "scenario.py",
        "package": {"file": "mautic-upgrade-7.2.3.sh", "sha256": "sha256:" + hashlib.sha256(package).hexdigest(), "size": len(package)},
        "dependencies": [],
        "download_url": "https://mcc.example/api/v1/agent/scenarios/debian/mautic-upgrade/7.2.3/artifact",
    }
    projection = scenario_sync._manifest_projection(family="debian", rows=[row])
    return {
        "schema": scenario_sync.SYNC_SCHEMA,
        "contract_version": scenario_sync.CONTRACT_VERSION,
        "family": "debian",
        "runtime_directory": "scenario",
        "replace": True,
        "manifest_sha256": "sha256:" + hashlib.sha256(scenario_sync._canonical(projection)).hexdigest(),
        "scenarios": [row],
    }


def _config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        state_db_path=str(tmp_path / "var" / "mcd-state.db"),
        mcd_install_dir=str(tmp_path / "opt" / "mcd"),
        mcc_url="https://mcc.example",
        mcc_token="token",
    )


def _runtime_release_fixture(root: Path) -> None:
    files: list[Path] = []
    for relative in (
        "mcd_agent/__init__.py",
        "mcd_agent/daemon.py",
        "mcd_agent/state_push.py",
        "mcd_agent/mcd_control.py",
        "requirements.txt",
        "wheel/mcd_agent.whl",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"runtime fixture\n")
        files.append(path)
    manifest = {
        "schema": "mcd-runtime-package-v1",
        "version": "1.7.39",
        "wheel": "wheel/mcd_agent.whl",
        "requirements": "requirements.txt",
        "payload_sha256": {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files
        },
    }
    (root / "mcd-runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_state_sync_verifies_and_activates_then_reuses_exact_cache(tmp_path, monkeypatch):
    config = _config(tmp_path)
    package = b"#!/bin/sh\nexit 0\n"
    payload = _payload(package)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, *_args):
            return package

    calls = []
    monkeypatch.setattr(scenario_sync.request, "urlopen", lambda *_args, **_kwargs: calls.append(True) or Response())
    first = scenario_sync.apply_sync(config, payload)
    assert first["status"] == "activated"
    assert calls == [True]
    runtime = scenario_sync.read_runtime(config)
    assert runtime["manifest_sha256"] == payload["manifest_sha256"]
    local = scenario_sync.local_scenario(config, "mautic-upgrade")
    assert local["pin"]["artifact_media_type"] == "application/x-sh"
    assert local["pin"]["artifact_digest"] == "sha256:" + hashlib.sha256(package).hexdigest()

    monkeypatch.setattr(scenario_sync.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cache miss")))
    second = scenario_sync.apply_sync(config, payload)
    assert second["status"] == "already_current"


def test_synced_scenario_cache_does_not_break_next_config_load(tmp_path, monkeypatch):
    from mcd_agent import config as config_module

    config = _config(tmp_path)
    runtime = Path(config.mcd_install_dir) / "runtime"
    _runtime_release_fixture(runtime)
    package = b"#!/bin/sh\nexit 0\n"
    payload = _payload(package)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, *_args):
            return package

    monkeypatch.setattr(scenario_sync.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert scenario_sync.apply_sync(config, payload)["status"] == "activated"
    monkeypatch.setattr(config_module, "DEFAULT_RUNTIME_ARTIFACT_DIR", str(runtime))
    config_path = tmp_path / "mcd.toml"
    config_path.write_text(
        "[mcc]\nurl=\"https://mcc.example\"\ntoken=\"token\"\n"
        "[state]\nbackend=\"sqlite\"\n",
        encoding="utf-8",
    )

    loaded = config_module.load_config(str(config_path), allow_recover_from_mcc=False)

    assert loaded.mcc_url == "https://mcc.example"
    assert scenario_sync.local_scenario(config, "mautic-upgrade")["pin"]["artifact_digest"].startswith("sha256:")


def test_state_sync_does_not_replace_active_runtime_after_digest_failure(tmp_path, monkeypatch):
    config = _config(tmp_path)
    package = b"#!/bin/sh\nexit 0\n"
    payload = _payload(package)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, *_args):
            return package

    monkeypatch.setattr(scenario_sync.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert scenario_sync.apply_sync(config, payload)["status"] == "activated"
    original = scenario_sync.read_runtime(config)

    changed = json.loads(json.dumps(payload))
    changed["scenarios"][0]["version"] = "7.2.4"
    changed["scenarios"][0]["package"]["sha256"] = "0" * 64
    changed["manifest_sha256"] = "sha256:" + hashlib.sha256(scenario_sync._canonical(scenario_sync._manifest_projection(family="debian", rows=changed["scenarios"]))).hexdigest()
    monkeypatch.setattr(scenario_sync.request, "urlopen", lambda *_args, **_kwargs: Response())
    failed = scenario_sync.apply_sync(config, changed)
    assert failed["status"] == "failed"
    assert scenario_sync.read_runtime(config)["manifest_sha256"] == original["manifest_sha256"]


def test_state_push_control_consumes_scenario_sync(monkeypatch, tmp_path):
    from mcd_agent import state_push

    config = _config(tmp_path)
    received = []
    monkeypatch.setattr(state_push, "apply_scenario_sync", lambda cfg, value: received.append((cfg, value)) or {"status": "activated"})
    state_push._ingest_control_response(
        config.state_db_path,
        {"agent_id": "agent-1", "host_fingerprint": "sha256:" + "a" * 64},
        {
            "schema": "mcd-control-v1",
            "contract_version": "1.0",
            "commands": [],
            "scenario_sync": {"schema": scenario_sync.SYNC_SCHEMA, "contract_version": "1.0"},
        },
        config,
    )
    assert received == [(config, {"schema": scenario_sync.SYNC_SCHEMA, "contract_version": "1.0"})]
