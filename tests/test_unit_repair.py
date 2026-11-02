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
                repair_module, "_runtime_probe", return_value={"metadata_version": "1.6.40", "imported_version": "1.6.40", "imported_path": str(install / "venv" / "lib" / "mcd_agent" / "__init__.py")}
            ), patch.object(repair_module, "_scheduler_status", return_value={"paused": False, "running_tasks": 0, "tracked_running_tasks": 0, "external_running_tasks": 0}), patch.object(
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

    def test_failure_rolls_back_dropin_and_restarts_for_readback(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            install, systemd = self._layout(root)
            dropin = systemd / "mcd.service.d" / repair_module.DROPIN_NAME
            dropin.parent.mkdir()
            dropin.write_text("[Service]\nWorkingDirectory=/old\nKillMode=process\n", encoding="utf-8")
            with patch.object(repair_module, "_show", return_value=""), patch.object(repair_module, "_run") as run, patch.object(
                repair_module, "_runtime_probe", return_value={"metadata_version": "1.6.40", "imported_version": "1.6.40", "imported_path": str(install / "venv" / "lib" / "mcd_agent" / "__init__.py")}
            ), patch.object(repair_module, "_scheduler_status", return_value={"paused": False, "running_tasks": 0, "tracked_running_tasks": 0, "external_running_tasks": 0}), patch.object(
                repair_module, "_health", side_effect=repair_module.RepairError("health failed")
            ):
                with self.assertRaisesRegex(repair_module.RepairError, "health failed"):
                    repair_module.repair(
                        install_dir=install, service="mcd", systemctl="systemctl",
                        systemd_dir=systemd, timeout=1, allow_nonproduction=True,
                    )
            self.assertEqual(dropin.read_text(encoding="utf-8"), "[Service]\nWorkingDirectory=/old\nKillMode=process\n")
            self.assertTrue(any(call.args[0][1:3] == ["restart", "mcd"] for call in run.call_args_list if call.args))
            receipts = list((install / "var" / "repair").glob("mcd-unit-repair-failed-*.json"))
            self.assertTrue(receipts)
            failure = receipts[0].read_text(encoding="utf-8")
            self.assertIn('"error": "health failed"', failure)
            self.assertIn('"readback"', failure)

    def test_scheduler_gate_is_status_only_until_operations_pilot_cycle(self) -> None:
        before = {"paused": False, "running_tasks": 0, "tracked_tasks": [], "external_tasks": []}
        gate = repair_module._scheduler_gate(before, dict(before))
        self.assertEqual(gate["status_probe"], "ok")
        self.assertEqual(gate["completion_probe"], "not_executed")
        self.assertTrue(gate["pilot_cycle_required"])

    def test_scheduler_status_accepts_warning_prefixed_json(self) -> None:
        completed = type("Completed", (), {"returncode": 0, "stdout": 'WARNING stale task reconciled\\n{"paused": false, "running_tasks": 0, "tracked_running_tasks": 0, "external_running_tasks": 0}', "stderr": ""})()
        with patch.object(repair_module.subprocess, "run", return_value=completed):
            result = repair_module._scheduler_status(Path("/opt/mcd/venv/bin/python"), Path("/opt/mcd"), Path("/opt/mcd/etc/mcd.toml"))
        self.assertEqual(result["running_tasks"], 0)


if __name__ == "__main__":
    unittest.main()
