from __future__ import annotations

import builtins
import importlib
import os
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from mcd_agent.self_update import assert_production_runtime_origin


def test_self_update_imports_tomli_when_python_has_no_tomllib(monkeypatch):
    real_import = builtins.__import__
    original_module = sys.modules["mcd_agent.self_update"]
    fallback = ModuleType("tomli")

    def without_stdlib_tomllib(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tomllib":
            raise ModuleNotFoundError("simulated Python 3.10")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", without_stdlib_tomllib)
    monkeypatch.setitem(sys.modules, "tomli", fallback)
    monkeypatch.delitem(sys.modules, "mcd_agent.self_update", raising=False)
    try:
        module = importlib.import_module("mcd_agent.self_update")

        assert module.tomllib.__name__ == "tomli"
    finally:
        sys.modules["mcd_agent.self_update"] = original_module


class ProductionRuntimeBoundaryTests(unittest.TestCase):
    def test_production_rejects_source_pythonpath(self) -> None:
        with patch.dict(os.environ, {"PYTHONPATH": "/opt/mcd/src"}):
            with self.assertRaisesRegex(RuntimeError, "must not use the forbidden checkout"):
                assert_production_runtime_origin("/opt/mcd")

    def test_runtime_package_contains_no_forbidden_checkout_literal(self) -> None:
        package = Path(__file__).parents[1] / "mcd_agent"
        offenders = [
            str(path.relative_to(package))
            for path in package.rglob("*")
            if path.is_file()
            and path.suffix in {".py", ".json", ".toml"}
            and "/opt/mcd/src" in path.read_text(encoding="utf-8", errors="ignore")
        ]
        self.assertEqual(offenders, [])

    def test_shipped_unit_and_rule_have_no_source_dependency(self) -> None:
        root = Path(__file__).parents[1]
        unit = (root / "systemd" / "mcd-agent.service").read_text(encoding="utf-8")
        rule = (root / "docs" / "mcd-production-runtime-boundary.md").read_text(encoding="utf-8")
        self.assertIn("WorkingDirectory=/opt/mcd", unit)
        self.assertIn("KillMode=process", unit)
        self.assertNotIn("/opt/mcd/src", unit)
        self.assertIn("forbidden production dependencies", rule)


if __name__ == "__main__":
    unittest.main()
