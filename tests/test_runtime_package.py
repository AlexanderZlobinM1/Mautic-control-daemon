from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

from mcd_agent import self_update


class RuntimePackageTests(unittest.TestCase):
    def test_runtime_manifest_is_hash_bound_and_rejects_checkout_material(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcd-runtime-manifest-") as tmp:
            root = Path(tmp)
            package = root / "mcd_agent"
            package.mkdir()
            init = package / "__init__.py"
            init.write_text('__version__ = "1.5.134"\n', encoding="utf-8")
            requirements = root / "requirements.txt"
            requirements.write_text("PyMySQL>=1.1.1\n", encoding="utf-8")
            wheel_dir = root / "wheel"
            wheel_dir.mkdir()
            wheel = wheel_dir / "mcd_agent-1.5.134-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("mcd_agent/__init__.py", init.read_bytes())
            payload = {}
            for path in (init, requirements, wheel):
                payload[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            (root / "mcd-runtime-manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "mcd-runtime-package-v1",
                        "version": "1.5.134",
                        "wheel": "wheel/mcd_agent-1.5.134-py3-none-any.whl",
                        "requirements": "requirements.txt",
                        "payload_sha256": payload,
                    }
                ),
                encoding="utf-8",
            )

            manifest = self_update._runtime_manifest(root)
            self.assertEqual(manifest["version"], "1.5.134")
            self.assertEqual(self_update._runtime_wheel(root), wheel.resolve())
            (root / "README.md").write_text("checkout material\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                self_update._runtime_manifest(root)

    def test_builder_emits_runtime_only_archive_with_detached_provenance(self) -> None:
        root = Path(__file__).parents[1]
        builder = root / "tools" / "build-runtime-archive.py"
        canonical_commit = subprocess.check_output(
            ["git", "rev-parse", "origin/main"],
            cwd=root,
            text=True,
        ).strip()
        with tempfile.TemporaryDirectory(prefix="mcd-runtime-test-") as tmp:
            archive = Path(tmp) / "mcd-agent-runtime.tar.gz"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(builder),
                    "--source",
                    str(root),
                    "--output",
                    str(archive),
                    "--source-commit",
                    canonical_commit,
                    "--source-archive-sha256",
                    "a" * 64,
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
            self.assertTrue(archive.is_file())

            with tarfile.open(archive, "r:gz") as tar:
                names = tar.getnames()
                self.assertIn("mcd-runtime-manifest.json", names)
                self.assertIn("mcd_agent/__init__.py", names)
                wheel_names = [name for name in names if name.startswith("wheel/") and name.endswith(".whl")]
                self.assertEqual(len(wheel_names), 1)
                forbidden = {
                    "tests",
                    "docs",
                    "README.md",
                    "ALGORITHM.md",
                    "TEST_ALGORITHM.md",
                    "CHANGELOG.md",
                    "pyproject.toml",
                }
                self.assertFalse(
                    [name for name in names if name.split("/", 1)[0] in forbidden or name.endswith((".pyc", "/__pycache__"))]
                )
                manifest = json.loads(tar.extractfile("mcd-runtime-manifest.json").read())  # type: ignore[union-attr]
                self.assertEqual(manifest["schema"], "mcd-runtime-package-v1")
                self.assertEqual(manifest["source_commit"], canonical_commit)
                self.assertNotIn("source_archive_sha256", manifest)

                wheel_data = tar.extractfile(wheel_names[0]).read()  # type: ignore[union-attr]
                with tempfile.TemporaryDirectory(prefix="mcd-wheel-test-") as wheel_tmp:
                    wheel_path = Path(wheel_tmp) / Path(wheel_names[0]).name
                    wheel_path.write_bytes(wheel_data)
                    with zipfile.ZipFile(wheel_path) as wheel:
                        wheel_entries = wheel.namelist()
                        self.assertIn("mcd_agent/__init__.py", wheel_entries)
                        self.assertFalse(any(entry.startswith(("tests/", "docs/")) for entry in wheel_entries))

            receipt = json.loads(Path(str(archive) + ".receipt.json").read_text(encoding="utf-8"))
            self.assertTrue(receipt["runtime_only"])
            self.assertEqual(receipt["source_commit"], canonical_commit)
            self.assertEqual(receipt["source_archive_sha256"], "a" * 64)
            self.assertEqual(len(receipt["archive_sha256"]), 64)

    def test_builder_rejects_noncanonical_source_commit(self) -> None:
        root = Path(__file__).parents[1]
        builder = root / "tools" / "build-runtime-archive.py"
        with tempfile.TemporaryDirectory(prefix="mcd-runtime-provenance-") as tmp:
            archive = Path(tmp) / "mcd-agent-runtime.tar.gz"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(builder),
                    "--source",
                    str(root),
                    "--output",
                    str(archive),
                    "--source-commit",
                    "0" * 40,
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("source commit mismatch", proc.stderr or proc.stdout)


if __name__ == "__main__":
    unittest.main()
