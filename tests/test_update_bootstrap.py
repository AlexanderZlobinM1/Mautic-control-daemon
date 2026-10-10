from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class UpdateBootstrapTests(unittest.TestCase):
    def test_legacy_1_5_75_verify_uses_venv_package_before_new_updater_runs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "mcd" / "src"
            site_dir = root / "mcd" / "venv" / "lib" / "python3.12" / "site-packages"
            source_dir.mkdir(parents=True)
            (source_dir / "mcd_agent").mkdir()
            (source_dir / "pyproject.toml").write_text("[project]\nname='mcd-agent'\n", encoding="utf-8")
            (source_dir / "sitecustomize.py").write_bytes(Path(__file__).parents[1].joinpath("sitecustomize.py").read_bytes())
            (source_dir / "mcd_agent" / "__init__.py").write_text('__version__ = "1.5.79"\n', encoding="utf-8")

            installed_package = site_dir / "mcd_agent"
            installed_package.mkdir(parents=True)
            (installed_package / "__init__.py").write_text('__version__ = "1.5.79"\n', encoding="utf-8")
            dist_info = site_dir / "mcd_agent-1.5.79.dist-info"
            dist_info.mkdir(parents=True)
            (dist_info / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: mcd-agent\nVersion: 1.5.79\n",
                encoding="utf-8",
            )
            (site_dir / "__editable__.mcd_agent-1.5.75.pth").write_text(
                "import __editable___mcd_agent_1_5_75_finder; __editable___mcd_agent_1_5_75_finder.install()\n",
                encoding="utf-8",
            )
            (site_dir / "__editable___mcd_agent_1_5_75_finder.py").write_text(
                "# stale editable finder\n",
                encoding="utf-8",
            )

            verify_script = (
                "import importlib.metadata as m, json, mcd_agent; "
                "print(json.dumps({'package': m.version('mcd-agent'), "
                "'imported': mcd_agent.__file__}))"
            )
            env = {
                "PATH": str(Path(sys.executable).parent),
                "PYTHONPATH": os_path(source_dir, site_dir),
            }
            proc = subprocess.run(
                [sys.executable, "-c", verify_script],
                cwd="/",
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
            result = json.loads(proc.stdout)
            self.assertEqual(result["package"], "1.5.79")
            self.assertEqual(Path(result["imported"]).resolve(), (installed_package / "__init__.py").resolve())
            self.assertFalse((site_dir / "__editable__.mcd_agent-1.5.75.pth").exists())
            self.assertFalse((site_dir / "__editable___mcd_agent_1_5_75_finder.py").exists())


def os_path(*paths: Path) -> str:
    return ":".join(str(path) for path in paths)


if __name__ == "__main__":
    unittest.main()
