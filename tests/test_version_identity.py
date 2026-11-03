from __future__ import annotations

import tempfile
import tomllib
import unittest
from importlib.metadata import version as package_version
from pathlib import Path
from unittest.mock import patch

from mcd_agent import __version__
from mcd_agent.version_identity import (
    agent_version_payload,
    installed_agent_version,
    runtime_source_revision,
    source_version,
)


class VersionIdentityTests(unittest.TestCase):
    def test_runtime_version_matches_package_metadata(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        pyproject = package_root / "pyproject.toml"
        if pyproject.is_file():
            with pyproject.open("rb") as handle:
                metadata_version = str(tomllib.load(handle)["project"]["version"])
        else:
            metadata_version = package_version("mcd-agent")

        self.assertEqual(__version__, metadata_version)

    def test_source_version_reads_installed_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pkg = root / "mcd_agent"
            pkg.mkdir()
            (pkg / "__init__.py").write_text('__version__ = "0.9.37"\n', encoding="utf-8")

            self.assertEqual(source_version(root), "0.9.37")
            self.assertEqual(installed_agent_version(root), __version__)

    def test_payload_keeps_running_source_and_package_versions_separate(self) -> None:
        with (
            tempfile.TemporaryDirectory() as td,
            patch("mcd_agent.version_identity.__version__", "0.9.75"),
            patch("mcd_agent.version_identity.package_agent_version", lambda: "0.9.39"),
        ):
            root = Path(td)
            pkg = root / "mcd_agent"
            pkg.mkdir()
            (pkg / "__init__.py").write_text('__version__ = "0.9.37"\n', encoding="utf-8")

            payload = agent_version_payload(root)

        self.assertEqual(payload["agent_version"], "0.9.75")
        self.assertEqual(payload["agent_running_version"], "0.9.75")
        self.assertEqual(payload["agent_installed_version"], "0.9.75")
        self.assertEqual(payload["agent_source_version"], "0.9.37")
        self.assertEqual(payload["agent_source_revision"], "")
        self.assertEqual(payload["agent_package_version"], "0.9.39")
        self.assertTrue(payload["agent_package_mismatch"])
        self.assertTrue(payload["agent_version_mismatch"])


if __name__ == "__main__":
    unittest.main()


def test_default_runtime_identity_never_reads_parallel_source(monkeypatch):
    import mcd_agent.version_identity as identity
    def forbidden(*args, **kwargs):
        raise AssertionError("runtime identity must not read a project tree")
    monkeypatch.setattr(Path, "read_text", forbidden)
    assert identity.source_version() is None
    assert identity.installed_agent_version() == identity.__version__
    assert identity.agent_version_payload()["agent_source_version"] == ""
    assert identity.agent_version_payload()["agent_source_revision"] == ""


def test_runtime_flags_detect_stale_package_metadata_without_source(monkeypatch):
    import mcd_agent.version_identity as identity
    monkeypatch.setattr(identity, '__version__', '1.7.0')
    monkeypatch.setattr(identity, 'package_agent_version', lambda: '1.6.38')
    payload = identity.agent_version_payload()
    assert payload['agent_source_version'] == ''
    assert payload['agent_source_revision'] == ''
    assert payload['agent_installed_version'] == '1.7.0'
    assert payload['agent_package_mismatch'] is True
    assert payload['agent_version_mismatch'] is True


def test_runtime_source_revision_reads_only_explicit_verified_manifest(tmp_path):
    manifest = tmp_path / "mcd-runtime-manifest.json"
    manifest.write_text(
        '{"schema":"mcd-runtime-package-v1",'
        '"source_commit":"D6720DCC37943B038E4F08AC6AB72DCD586805F5"}',
        encoding="utf-8",
    )

    assert runtime_source_revision(manifest) == "d6720dcc37943b038e4f08ac6ab72dcd586805f5"
    payload = agent_version_payload(runtime_manifest=manifest)
    assert payload["agent_source_version"] == ""
    assert payload["agent_source_revision"] == "d6720dcc37943b038e4f08ac6ab72dcd586805f5"
    assert payload["agent_version_mismatch"] is False


def test_runtime_source_revision_rejects_wrong_schema_and_non_commit(tmp_path):
    manifest = tmp_path / "mcd-runtime-manifest.json"
    manifest.write_text(
        '{"schema":"other","source_commit":"d6720dcc"}',
        encoding="utf-8",
    )
    assert runtime_source_revision(manifest) is None

    manifest.write_text(
        '{"schema":"mcd-runtime-package-v1","source_commit":"not-a-commit"}',
        encoding="utf-8",
    )
    assert runtime_source_revision(manifest) is None
