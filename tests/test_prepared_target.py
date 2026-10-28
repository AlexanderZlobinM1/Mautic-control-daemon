import hashlib
import io
import json
from pathlib import Path
import unittest
import zipfile
from unittest.mock import patch

from mcd_agent.prepared_target import (
    MAX_INPUT_BYTES,
    MANIFEST_NAME,
    PRESERVED_PATHS,
    PreparedTargetError,
    download_prepared_target,
    materialize_prepared_target,
    merge_into_live,
    prepared_target_php,
    validate_prepared_target,
    validate_prepared_target_runtime,
)


def _fixture(tmp_path: Path):
    files = {
        "app/release_metadata.json": b'{"version":"7.1.4"}\n',
        "app/target.php": b"prepared target\n",
        "plugins/PreparedBundle/Bundle.php": b"<?php\n",
        "bin/console": b"#!/usr/bin/env php\n",
    }
    manifest = {
        "schema": "mcd-mautic-prepared-target-manifest-v1",
        "target_version": "7.1.4",
        "install_type": "composer",
        "application_root_relative": ".",
        "preserve_paths": list(PRESERVED_PATHS),
        "files": [
            {"path": path, "sha256": hashlib.sha256(value).hexdigest()}
            for path, value in sorted(files.items())
        ],
    }
    artifact = io.BytesIO()
    with zipfile.ZipFile(artifact, "w") as archive:
        for path, value in files.items():
            archive.writestr(path, value)
        archive.writestr(MANIFEST_NAME, json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    body = artifact.getvalue()
    prepared = {
        "schema": "mcd-mautic-prepared-target-input-v1",
        "prepared_target_id": "mautic-7.1.4-php8.4-composer",
        "target_version": "7.1.4",
        "install_type": "composer",
        "artifact": {
            "path": "/api/v1/agent/mautic/prepared-targets/fixture.zip",
            "grant": "grant-fixture",
            "size_bytes": len(body),
            "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
            "media_type": "application/zip",
        },
        "manifest": manifest,
    }
    return prepared, body


def _version(root: Path):
    return json.loads((root / "app/release_metadata.json").read_text())["version"]


class PreparedTargetTests(unittest.TestCase):
  def test_input_limit_covers_full_mcc_manifest_envelope(self):
    self.assertGreaterEqual(MAX_INPUT_BYTES, 4 * 1024 * 1024)

  def test_dev_candidate_metadata_keeps_approved_baseline(self):
    from mcd_agent import __version__

    contract = json.loads(
        (Path(__file__).parents[1] / "mcd_agent/contracts/mautic-prepared-target-v1.json").read_text()
    )
    self.assertEqual(__version__, "1.6.0")
    self.assertEqual(contract["candidate"], {
        "agent_version": "1.6.0",
        "lineage": "current-mcd-prepared-target",
        "approved_baseline": "1.5.268",
        "baseline_role": "historical-rollback-audit-only",
        "publication_status": "dev-only",
    })

  def test_prepared_target_identity_is_target_only(self):
    prepared, _body = _fixture(Path("/tmp"))
    self.assertEqual(validate_prepared_target(prepared)["prepared_target_id"], "mautic-7.1.4-php8.4-composer")
    zip_input = json.loads(json.dumps(prepared))
    zip_input["install_type"] = "zip"
    zip_input["prepared_target_id"] = "mautic-7.1.4-zip"
    zip_input["manifest"]["install_type"] = "zip"
    self.assertEqual(validate_prepared_target(zip_input)["prepared_target_id"], "mautic-7.1.4-zip")
    invalid = json.loads(json.dumps(prepared))
    invalid["prepared_target_id"] = "mautic-7.1.4-composer"
    with self.assertRaisesRegex(PreparedTargetError, "prepared_target_id_invalid"):
        validate_prepared_target(invalid)

  def test_composer_prepared_target_requires_exact_runtime_php_line(self):
    prepared, _body = _fixture(Path("/tmp"))
    self.assertEqual(prepared_target_php(prepared["prepared_target_id"]), "8.4")
    self.assertEqual(validate_prepared_target_runtime(prepared, "8.4")["target_version"], "7.1.4")
    with self.assertRaisesRegex(PreparedTargetError, "prepared_target_runtime_php_mismatch"):
        validate_prepared_target_runtime(prepared, "8.3")
    with self.assertRaisesRegex(PreparedTargetError, "prepared_target_runtime_php_unavailable"):
        validate_prepared_target_runtime(prepared, "")

  def test_zip_prepared_target_has_no_php_matrix_binding(self):
    prepared, _body = _fixture(Path("/tmp"))
    prepared = json.loads(json.dumps(prepared))
    prepared["install_type"] = "zip"
    prepared["prepared_target_id"] = "mautic-7.1.4-zip"
    prepared["manifest"]["install_type"] = "zip"
    self.assertEqual(validate_prepared_target_runtime(prepared, "8.3")["install_type"], "zip")

  def test_prepared_target_verifies_manifest_and_preserves_config_var(self):
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as raw:
      tmp_path = Path(raw)
      prepared, body = _fixture(tmp_path)
    target = materialize_prepared_target(prepared, body, read_version=_version)
    try:
        live = tmp_path / "live"
        (live / "app").mkdir(parents=True)
        (live / "plugins").mkdir()
        (live / "bin").mkdir()
        (live / "config").mkdir()
        (live / "var").mkdir()
        (live / "config/local.php").write_text("original config")
        (live / "var/state.json").write_text("original var")
        (live / "app/target.php").write_text("old target")

        merge_into_live(target.root, live)

        assert (live / "app/target.php").read_bytes() == b"prepared target\n"
        assert (live / "config/local.php").read_text() == "original config"
        assert (live / "var/state.json").read_text() == "original var"
    finally:
        target.close()

  def test_prepared_target_rejects_manifest_or_payload_drift(self):
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as raw:
      tmp_path = Path(raw)
      prepared, body = _fixture(tmp_path)
    bad = json.loads(json.dumps(prepared))
    bad["manifest"]["files"][0]["sha256"] = "0" * 64
    with self.assertRaisesRegex(PreparedTargetError, "file_digest_mismatch|manifest_mismatch"):
        materialize_prepared_target(bad, body, read_version=_version)

    with self.assertRaisesRegex(PreparedTargetError, "integrity_invalid"):
        materialize_prepared_target(prepared, body + b"drift", read_version=_version)

  def test_prepared_target_download_uses_existing_mcc_bearer_transport(self):
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as raw:
      tmp_path = Path(raw)
      prepared, body = _fixture(tmp_path)
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return body

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization")
        seen["grant"] = request.get_header("X-mcc-artifact-grant")
        seen["timeout"] = timeout
        return Response()

    result = download_prepared_target(
        type("Config", (), {"mcc_url": "https://mcc.example", "mcc_token": "api-key"})(),
        prepared,
        opener=opener,
    )
    self.assertEqual(result, body)
    self.assertEqual(seen, {
        "url": "https://mcc.example/api/v1/agent/mautic/prepared-targets/fixture.zip",
        "authorization": "Bearer api-key",
        "grant": "grant-fixture",
        "timeout": 120,
    })

  def test_prepared_target_requires_https_mcc_and_exact_shape(self):
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as raw:
      tmp_path = Path(raw)
      prepared, _body = _fixture(tmp_path)
    with self.assertRaisesRegex(PreparedTargetError, "mcc_transport_required"):
        download_prepared_target(
            type("Config", (), {"mcc_url": "http://mcc.example", "mcc_token": "api-key"})(),
            prepared,
            opener=lambda *_args, **_kwargs: self.fail("network fallback must not run"),
        )
    with self.assertRaisesRegex(PreparedTargetError, "fields_invalid"):
        validate_prepared_target({**prepared, "unexpected": True})

  def test_upgrade_apply_uses_prepared_files_without_host_composer(self):
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace
    try:
      from mcd_agent import mautic_upgrade as upgrade
    except ModuleNotFoundError as exc:
      self.skipTest(f"full upgrade dependency unavailable: {exc.name}")

    with TemporaryDirectory() as raw:
      tmp_path = Path(raw)
      prepared, body = _fixture(tmp_path)
      target = materialize_prepared_target(prepared, body, read_version=_version)
      try:
        live = tmp_path / "live"
        (live / "app").mkdir(parents=True)
        (live / "plugins").mkdir()
        (live / "bin").mkdir()
        (live / "config").mkdir()
        (live / "var").mkdir()
        (live / "bin/console").write_bytes(b"console")
        (live / "app/release_metadata.json").write_text('{"version":"7.1.3"}\n')
        (live / "config/local.php").write_text("keep config")
        (live / "var/state.json").write_text("keep var")
        commands = []
        with patch.object(upgrade, "_run", side_effect=lambda command, **_kwargs: commands.append(command)), \
             patch.object(upgrade, "_doctrine_migrate_command", return_value="doctrine:migrations:migrate"), \
             patch.object(upgrade, "_run_doctrine_migrate_with_reconcile"), \
             patch.object(upgrade, "_verify_or_reconcile_doctrine_migrations"), \
             patch.object(upgrade, "_patch_lifecycle_phase"):
          upgrade._apply_prepared_target(
              SimpleNamespace(), str(live), "bin/console", "php", "7.1.4", target,
          )
        self.assertTrue(all("composer" not in " ".join(command) for command in commands))
        self.assertEqual((live / "app/target.php").read_bytes(), b"prepared target\n")
        self.assertEqual((live / "config/local.php").read_text(), "keep config")
        self.assertEqual((live / "var/state.json").read_text(), "keep var")
      finally:
        target.close()
