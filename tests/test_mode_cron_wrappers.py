import tempfile
import unittest
from pathlib import Path

from mcd_agent.mode import _comment_managed


class ModeCronWrapperTests(unittest.TestCase):
    def test_managed_mautic_wrapper_script_is_commented(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "mautic-segment-update.sh"
            script.write_text(
                "#!/bin/bash\n"
                'CMD="php /var/www/example/public_html/bin/console mautic:segments:update --batch-limit=1000"\n'
                '$CMD >> /var/www/example/public_html/var/logs/mautic-segment.log 2>&1\n',
                encoding="utf-8",
            )
            content = f"0,20,40 4-23 * * * {script}\n"

            updated, changed = _comment_managed(content, "ts")

        self.assertEqual(changed, 1)
        self.assertIn("# MCD_MANAGED ts: disabled by mcd profile=active", updated)
        self.assertIn(f"# 0,20,40 4-23 * * * {script}", updated)

    def test_unrelated_wrapper_script_is_left_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "daily-report.sh"
            script.write_text("#!/bin/bash\n/bin/true\n", encoding="utf-8")
            content = f"0 5 * * * {script}\n"

            updated, changed = _comment_managed(content, "ts")

        self.assertEqual(changed, 0)
        self.assertEqual(updated, content)


if __name__ == "__main__":
    unittest.main()
