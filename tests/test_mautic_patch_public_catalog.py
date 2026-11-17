import base64
from copy import deepcopy
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mcd_agent import mautic_patch_runtime
from mcd_agent.mautic_patch_public_catalog import (
    CATALOG_SCHEMA,
    REF_SCHEMA,
    resolve_public_pre_reload_plan,
)
from mcd_agent.mautic_patch_resolution import canonical_json_sha256
from mcd_agent.plugins import _run_plugin_install_reload


REGISTRY_COMMIT = "a" * 40
REGISTRY_SHA256 = "b" * 64


def _projection() -> dict:
    payload = (
        "diff --git a/app/fixture.txt b/app/fixture.txt\n"
        "--- a/app/fixture.txt\n"
        "+++ b/app/fixture.txt\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n"
    ).encode()
    return {
        "schema": CATALOG_SCHEMA,
        "registry_commit": REGISTRY_COMMIT,
        "registry_sha256": REGISTRY_SHA256,
        "selectors": [
            {
                "trigger": "daemon_reconcile",
                "phase": "before_plugin_reload",
                "observed_majors": [4],
                "version_ranges": [">=4.4.0 <=4.4.13"],
                "install_types": ["zip"],
                "plan_template": {
                    "schema": "mcd-mautic-patch-plan-v3",
                    "registry_commit": REGISTRY_COMMIT,
                    "registry_sha256": REGISTRY_SHA256,
                    "patches": [
                        {
                            "id": "FIXTURE-PRE-RELOAD-GUARD",
                            "execution_kind": "git_patch_v1",
                            "triggers": ["daemon_reconcile"],
                            "phase_order": 10,
                            "phases": ["before_plugin_reload"],
                            "source_paths": ["app/fixture.txt"],
                            "payload_path": "fixtures/pre-reload.patch",
                            "gate_logic": "vulnerable_exactly_one; fixed_exactly_one; mixed_or_unknown=error",
                            "gate": [
                                {
                                    "group": "vulnerable",
                                    "kind": "exact_count",
                                    "path": "app/fixture.txt",
                                    "needle": "before",
                                    "expected_count": 1,
                                },
                                {
                                    "group": "fixed",
                                    "kind": "exact_count",
                                    "path": "app/fixture.txt",
                                    "needle": "after",
                                    "expected_count": 1,
                                },
                            ],
                        }
                    ],
                    "payloads": [
                        {
                            "path": "fixtures/pre-reload.patch",
                            "sha256": hashlib.sha256(payload).hexdigest(),
                            "content_base64": base64.b64encode(payload).decode(),
                        }
                    ],
                },
            }
        ],
    }


@contextmanager
def _catalog_server(
    *,
    required: bool = True,
    projection: dict | None = None,
    ref_override: dict | None = None,
    serve_catalog: bool = True,
):
    projection = deepcopy(projection) if projection is not None else _projection()
    ref = {
        "schema": REF_SCHEMA,
        "url": "/patches/catalog.json",
        "sha256": canonical_json_sha256(projection),
        "required": required,
    }
    if ref_override is not None:
        ref = deepcopy(ref_override)
    manifest = {
        "manifest_version": 1,
        "mautic_major": 4,
        "plugins": [],
        "mautic_patch_catalog": ref,
    }
    payloads = {
        "/mauticctl/packages/mautic4/manifest.json": json.dumps(manifest).encode(),
    }
    if serve_catalog:
        payloads["/patches/catalog.json"] = json.dumps(projection).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = payloads.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _scenario(tmp_path: Path, base_url: str, *, registered: bool = False):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "console").write_text("#!/usr/bin/env php\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "plugins").mkdir()
    source = tmp_path / "app" / "fixture.txt"
    source.write_text("before\n")
    config = SimpleNamespace(
        plugins_repo_base_url=base_url,
        plugins_manifest_path_template="/mauticctl/packages/mautic{major}/manifest.json",
        plugins_repo_fallback_ip=None,
        mcc_url="https://management.invalid" if registered else "",
        mcc_token="registered-token" if registered else None,
        mcc_host_name="fixture-host" if registered else None,
        php_bin="php",
        mautic_run_as_user="www-data",
    )
    install = SimpleNamespace(
        root=str(tmp_path),
        instance_uid="fixture.example",
        mautic_major=4,
        db=None,
        console_path=str(tmp_path / "bin" / "console"),
        local_php_path=None,
    )
    return config, install, source


def _version_evidence(version="4.4.13"):
    return patch.multiple(
        "mcd_agent.mautic_patch_runtime",
        read_mautic_version_evidence_read_only=lambda _root: {"version": version},
        confirmed_mautic_major=lambda *_args, **_kwargs: 4,
    )


def test_unregistered_plugin_install_applies_real_public_plan_before_native_reload(tmp_path):
    with _catalog_server() as base_url:
        config, install, source = _scenario(tmp_path, base_url)
        events = []

        def native(*_args, **_kwargs):
            assert source.read_text() == "after\n"
            events.append("native")
            return 0, "plugins reloaded"

        with _version_evidence("4.4.0"), patch(
            "mcd_agent.plugins._run_plugin_template", side_effect=native
        ):
            _run_plugin_install_reload(config, install, expected_bundles={"ExampleBundle"})

    assert events == ["native"]
    receipts = list((tmp_path / ".mcd" / "patch-plan-v3").glob("*.evidence.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"] == "success"
    assert receipt["management_delivery"] == {"status": "not_configured"}
    assert receipt["evidence"]["selected"] == ["FIXTURE-PRE-RELOAD-GUARD"]


def test_public_plan_fixed_source_is_safe_no_write_before_native_reload(tmp_path):
    with _catalog_server() as base_url:
        config, install, source = _scenario(tmp_path, base_url)
        source.write_text("after\n")
        before = source.stat().st_mtime_ns
        with _version_evidence(), patch(
            "mcd_agent.plugins._run_plugin_template", return_value=(0, "plugins reloaded")
        ) as native:
            _run_plugin_install_reload(config, install, expected_bundles={"ExampleBundle"})
    assert source.read_text() == "after\n"
    assert source.stat().st_mtime_ns == before
    native.assert_called_once()


def test_public_plan_unknown_source_fails_closed_before_native_reload(tmp_path):
    with _catalog_server() as base_url:
        config, install, source = _scenario(tmp_path, base_url)
        source.write_text("operator drift\n")
        with _version_evidence(), patch("mcd_agent.plugins._run_plugin_template") as native:
            with pytest.raises(RuntimeError, match="Catalog plugin reload preflight failed"):
                _run_plugin_install_reload(config, install, expected_bundles={"ExampleBundle"})
    assert source.read_text() == "operator drift\n"
    native.assert_not_called()


def test_registered_delivery_failure_does_not_replay_or_fail_local_execution(tmp_path):
    with _catalog_server() as base_url:
        config, install, source = _scenario(tmp_path, base_url, registered=True)
        with _version_evidence(), patch(
            "mcd_agent.mautic_patch_runtime.report_evidence",
            side_effect=OSError("management offline"),
        ), patch(
            "mcd_agent.mautic_patch_runtime.resolve_plan",
            side_effect=AssertionError("management resolver must not be used"),
        ), patch(
            "mcd_agent.plugins._run_plugin_template", return_value=(0, "plugins reloaded")
        ) as native:
            _run_plugin_install_reload(config, install, expected_bundles={"ExampleBundle"})
    assert source.read_text() == "after\n"
    native.assert_called_once()
    receipt_path = next((tmp_path / ".mcd" / "patch-plan-v3").glob("*.evidence.json"))
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "success"
    assert receipt["management_delivery"] == {"status": "deferred", "reason": "OSError"}


def test_required_public_catalog_unavailable_blocks_before_native_reload(tmp_path):
    with _catalog_server(serve_catalog=False) as base_url:
        config, install, source = _scenario(tmp_path, base_url)
        with _version_evidence(), patch("mcd_agent.plugins._run_plugin_template") as native:
            with pytest.raises(RuntimeError, match="required_public_patch_catalog_unavailable"):
                _run_plugin_install_reload(config, install, expected_bundles={"ExampleBundle"})
    assert source.read_text() == "before\n"
    native.assert_not_called()


def test_optional_public_catalog_unavailable_is_valid_noop(tmp_path):
    with _catalog_server(required=False, serve_catalog=False) as base_url:
        config, install, source = _scenario(tmp_path, base_url)
        with _version_evidence(), patch(
            "mcd_agent.plugins._run_plugin_template", return_value=(0, "plugins reloaded")
        ) as native:
            _run_plugin_install_reload(config, install, expected_bundles={"ExampleBundle"})
    assert source.read_text() == "before\n"
    native.assert_called_once()


@pytest.mark.parametrize("version", ["4.3.9", "4.4.14"])
def test_known_version_outside_owner_range_is_safe_noop(tmp_path, version):
    with _catalog_server() as base_url:
        config, install, source = _scenario(tmp_path, base_url)
        with _version_evidence(version), patch(
            "mcd_agent.plugins._run_plugin_template", return_value=(0, "plugins reloaded")
        ) as native:
            _run_plugin_install_reload(config, install, expected_bundles={"ExampleBundle"})
    assert source.read_text() == "before\n"
    native.assert_called_once()


@pytest.mark.parametrize("version", [None, "4.4"])
def test_unknown_or_malformed_version_blocks_required_projection_before_native_reload(tmp_path, version):
    with _catalog_server() as base_url:
        config, install, source = _scenario(tmp_path, base_url)
        with _version_evidence(version), patch("mcd_agent.plugins._run_plugin_template") as native:
            with pytest.raises(RuntimeError, match="required_public_patch_version_unknown"):
                _run_plugin_install_reload(config, install, expected_bundles={"ExampleBundle"})
    assert source.read_text() == "before\n"
    native.assert_not_called()


@pytest.mark.parametrize("shape", ["missing", "ambiguous"])
def test_required_public_projection_missing_or_ambiguous_blocks(tmp_path, shape):
    projection = _projection()
    if shape == "missing":
        projection["selectors"] = []
        reason = "required_public_patch_projection_missing"
    else:
        projection["selectors"].append(deepcopy(projection["selectors"][0]))
        reason = "required_public_patch_projection_ambiguous"
    with _catalog_server(projection=projection) as base_url:
        config, install, source = _scenario(tmp_path, base_url)
        with _version_evidence(), patch("mcd_agent.plugins._run_plugin_template") as native:
            with pytest.raises(RuntimeError, match=reason):
                _run_plugin_install_reload(config, install, expected_bundles={"ExampleBundle"})
    assert source.read_text() == "before\n"
    native.assert_not_called()


def test_public_projection_digest_mismatch_blocks(tmp_path):
    ref = {
        "schema": REF_SCHEMA,
        "url": "/patches/catalog.json",
        "sha256": "c" * 64,
        "required": True,
    }
    with _catalog_server(ref_override=ref) as base_url:
        config, install, source = _scenario(tmp_path, base_url)
        with _version_evidence(), patch("mcd_agent.plugins._run_plugin_template") as native:
            with pytest.raises(RuntimeError, match="public_patch_catalog_sha256_mismatch"):
                _run_plugin_install_reload(config, install, expected_bundles={"ExampleBundle"})
    assert source.read_text() == "before\n"
    native.assert_not_called()


def test_public_projection_malformed_ref_blocks(tmp_path):
    malformed = {
        "schema": REF_SCHEMA,
        "url": "/patches/catalog.json",
        "sha256": canonical_json_sha256(_projection()),
    }
    with _catalog_server(ref_override=malformed) as base_url:
        config, install, source = _scenario(tmp_path, base_url)
        with _version_evidence(), patch("mcd_agent.plugins._run_plugin_template") as native:
            with pytest.raises(RuntimeError, match="public_patch_catalog_ref_fields_invalid"):
                _run_plugin_install_reload(config, install, expected_bundles={"ExampleBundle"})
    assert source.read_text() == "before\n"
    native.assert_not_called()


def test_public_projection_malformed_version_range_blocks(tmp_path):
    projection = _projection()
    projection["selectors"][0]["version_ranges"] = ["4.4 through 4.5"]
    with _catalog_server(projection=projection) as base_url:
        config, install, source = _scenario(tmp_path, base_url)
        with _version_evidence(), patch("mcd_agent.plugins._run_plugin_template") as native:
            with pytest.raises(RuntimeError, match="public_patch_catalog_selector_version_ranges_invalid"):
                _run_plugin_install_reload(config, install, expected_bundles={"ExampleBundle"})
    assert source.read_text() == "before\n"
    native.assert_not_called()


def test_post_execution_metadata_failure_does_not_report_false_execution_error(tmp_path):
    with _catalog_server() as base_url:
        config, install, source = _scenario(tmp_path, base_url)
        with _version_evidence(), patch(
            "mcd_agent.mautic_patch_runtime._host_identity",
            side_effect=OSError("hostname unavailable"),
        ), patch(
            "mcd_agent.plugins._run_plugin_template", return_value=(0, "plugins reloaded")
        ) as native:
            _run_plugin_install_reload(config, install, expected_bundles={"ExampleBundle"})
    assert source.read_text() == "after\n"
    native.assert_called_once()
    receipt_path = next((tmp_path / ".mcd" / "patch-plan-v3").glob("*.evidence.json"))
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "success"


def test_registered_and_unregistered_use_identical_public_plan_template(tmp_path):
    with _catalog_server() as base_url:
        standalone_root = tmp_path / "standalone"
        registered_root = tmp_path / "registered"
        standalone_root.mkdir()
        registered_root.mkdir()
        standalone_config, standalone_install, _ = _scenario(standalone_root, base_url)
        registered_config, registered_install, _ = _scenario(registered_root, base_url, registered=True)
        common = {
            "trigger": "daemon_reconcile",
            "phase": "before_plugin_reload",
            "operation": "apply",
            "observed_version": "4.4.13",
            "observed_major": 4,
            "install_type": "zip",
            "run_id": "same-public-plan-001",
        }
        standalone = resolve_public_pre_reload_plan(standalone_config, standalone_install, **common)
        registered = resolve_public_pre_reload_plan(registered_config, registered_install, **common)
    assert standalone["plan"] == registered["plan"]
    assert standalone["plan_sha256"] == registered["plan_sha256"]
    assert standalone["catalog_sha256"] == registered["catalog_sha256"]


def test_post_execution_receipt_failure_is_typed_and_does_not_replay(tmp_path):
    with _catalog_server() as base_url:
        config, install, source = _scenario(tmp_path, base_url)
        results = []
        actual_run = mautic_patch_runtime.run

        def real_run(*args, **kwargs):
            result = actual_run(*args, **kwargs)
            results.append(result)
            return result

        with _version_evidence(), patch(
            "mcd_agent.mautic_patch_runtime._save_local_receipt",
            side_effect=OSError("disk full"),
        ), patch(
            "mcd_agent.mautic_patch_runtime.run",
            side_effect=real_run,
        ), patch(
            "mcd_agent.plugins._run_plugin_template", return_value=(0, "plugins reloaded")
        ) as native:
            _run_plugin_install_reload(config, install, expected_bundles={"ExampleBundle"})
    assert source.read_text() == "after\n"
    native.assert_called_once()
    assert results[0]["status"] == "success"
    assert results[0]["receipt_persistence"] == {
        "status": "failed",
        "reason": "OSError",
        "execution_completed": True,
    }
    assert not list((tmp_path / ".mcd" / "patch-plan-v3").glob("*.evidence.json"))
