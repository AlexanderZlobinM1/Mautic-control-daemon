from __future__ import annotations

import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "tools" / "mcd_unit_repair.py"
SPEC = importlib.util.spec_from_file_location("mcd_unit_repair", MODULE_PATH)
assert SPEC and SPEC.loader
repair_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair_module)


class UnitRepairTests(unittest.TestCase):
    def _layout(self, root: Path) -> tuple[Path, Path]:
        install = root / "opt" / "mcd"
        (install / "venv" / "bin").mkdir(parents=True)
        (install / "venv" / "bin" / "python").write_text("", encoding="utf-8")
        (install / "venv" / "bin" / "python").chmod(0o755)
        (install / "etc").mkdir()
        (install / "etc" / "mcd.toml").write_text("[mcd]\n", encoding="utf-8")
        systemd = root / "etc" / "systemd" / "system"
        systemd.mkdir(parents=True)
        (systemd / "mcd.service").write_text("[Service]\n", encoding="utf-8")
        return install, systemd

    def test_repair_writes_only_runtime_dropin_and_receipt(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            install, systemd = self._layout(root)
            with patch.object(repair_module, "_show", side_effect=lambda _s, _n, prop: {
                "WorkingDirectory": "/opt/mcd/src",
                "ExecStart": "/opt/mcd/venv/bin/python -m mcd_agent run --config /opt/mcd/etc/mcd.toml",
                "KillMode": "process",
            }.get(prop, "")), patch.object(repair_module, "_run"), patch.object(
                repair_module, "_health", return_value={"ActiveState": "active", "SubState": "running"}
            ):
                result = repair_module.repair(
                    install_dir=install, service="mcd", systemctl="systemctl",
                    systemd_dir=systemd, timeout=1, allow_nonproduction=True,
                )
            dropin = systemd / "mcd.service.d" / repair_module.DROPIN_NAME
            self.assertIn("WorkingDirectory=/opt/mcd", dropin.read_text(encoding="utf-8"))
            self.assertFalse((install / "src").exists())
            self.assertTrue(Path(str(result["receipt"])).is_file())

    def test_repair_refuses_source_checkout(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            install, systemd = self._layout(root)
            (install / "src").mkdir()
            with self.assertRaisesRegex(repair_module.RepairError, "forbidden"):
                repair_module.repair(
                    install_dir=install, service="mcd", systemctl="systemctl",
                    systemd_dir=systemd, timeout=1, allow_nonproduction=True,
                )


if __name__ == "__main__":
    unittest.main()
