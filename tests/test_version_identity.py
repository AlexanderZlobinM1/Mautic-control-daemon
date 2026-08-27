from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from mcd_agent import __version__
from mcd_agent.version_identity import agent_version_payload, installed_agent_version, source_version


class VersionIdentityTests(unittest.TestCase):
    def test_runtime_version_matches_package_metadata(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        with (package_root / "pyproject.toml").open("rb") as handle:
            package_version = str(tomllib.load(handle)["project"]["version"])

        self.assertEqual(__version__, package_version)

    def test_source_version_reads_installed_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pkg = root / "mcd_agent"
            pkg.mkdir()
            (pkg / "__init__.py").write_text('__version__ = "0.9.37"\n', encoding="utf-8")

            self.assertEqual(source_version(root), "0.9.37")
            self.assertEqual(installed_agent_version(root), "0.9.37")

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
        self.assertEqual(payload["agent_installed_version"], "0.9.37")
        self.assertEqual(payload["agent_source_version"], "0.9.37")
        self.assertEqual(payload["agent_package_version"], "0.9.39")
        self.assertTrue(payload["agent_package_mismatch"])
        self.assertTrue(payload["agent_version_mismatch"])


if __name__ == "__main__":
    unittest.main()
