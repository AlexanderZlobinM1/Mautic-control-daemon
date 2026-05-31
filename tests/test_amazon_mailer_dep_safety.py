from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mcd_agent.amazon_mailer_dep import _composer_project_is_mautic


class MailerComposerSafetyTests(unittest.TestCase):
    def test_rejects_unrelated_composer_project_created_by_mailer_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "composer.json").write_text(
                json.dumps({"require": {"symfony/sendgrid-mailer": "*"}}),
                encoding="utf-8",
            )
            self.assertFalse(_composer_project_is_mautic(str(root)))

    def test_accepts_mautic_composer_project(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "composer.json").write_text(
                json.dumps({"name": "mautic/recommended-project", "require": {"mautic/core-lib": "^4"}}),
                encoding="utf-8",
            )
            self.assertTrue(_composer_project_is_mautic(str(root)))


if __name__ == "__main__":
    unittest.main()
