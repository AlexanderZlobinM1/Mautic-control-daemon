import ast
import unittest
from pathlib import Path


class DaemonServiceProfileAutoApplyTests(unittest.TestCase):
    def test_daemon_service_profile_calls_are_advisory_dry_runs(self) -> None:
        source = Path(__file__).resolve().parents[1] / "mcd_agent" / "daemon.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))

        calls: list[ast.Call] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "service_profiles_apply_once":
                calls.append(node)

        self.assertGreaterEqual(len(calls), 1)
        for call in calls:
            dry_run_values = [kw.value for kw in call.keywords if kw.arg == "dry_run"]
            self.assertEqual(len(dry_run_values), 1)
            self.assertIsInstance(dry_run_values[0], ast.Constant)
            self.assertIs(dry_run_values[0].value, True)


if __name__ == "__main__":
    unittest.main()
