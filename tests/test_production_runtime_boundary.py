from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from mcd_agent.self_update import assert_production_runtime_origin


class ProductionRuntimeBoundaryTests(unittest.TestCase):
    def test_production_rejects_source_pythonpath(self) -> None:
        with patch.dict(os.environ, {"PYTHONPATH": "/opt/mcd/src"}):
            with self.assertRaisesRegex(RuntimeError, "must not use /opt/mcd/src"):
                assert_production_runtime_origin("/opt/mcd")

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
