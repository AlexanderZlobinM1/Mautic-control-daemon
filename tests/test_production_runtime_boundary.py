from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from mcd_agent.self_update import assert_production_runtime_origin


class ProductionRuntimeBoundaryTests(unittest.TestCase):
    def test_production_runtime_rejects_source_path_in_pythonpath(self) -> None:
        with patch.dict(os.environ, {"PYTHONPATH": "/opt/mcd/src"}):
            with self.assertRaisesRegex(RuntimeError, "must not use /opt/mcd/src"):
                assert_production_runtime_origin("/opt/mcd")

    def test_non_production_checkout_remains_testable(self) -> None:
        with patch.dict(os.environ, {"PYTHONPATH": str(Path(__file__).parents[1])}):
            assert_production_runtime_origin("/opt/mcd-dev-1.7")

    def test_shipped_unit_has_canonical_runtime_and_worker_preservation(self) -> None:
        root = Path(__file__).parents[1]
        unit = root / "systemd" / "mcd-agent.service"
        content = unit.read_text(encoding="utf-8")
        self.assertIn("WorkingDirectory=/opt/mcd", content)
        self.assertIn("ExecStart=/opt/mcd/venv/bin/python -m mcd_agent", content)
        self.assertIn("KillMode=process", content)
        self.assertNotIn("/opt/mcd/src", content)
        rule = (root / "docs" / "mcd-production-runtime-boundary.md").read_text(encoding="utf-8")
        self.assertIn("forbidden production dependencies", rule)


if __name__ == "__main__":
    unittest.main()
