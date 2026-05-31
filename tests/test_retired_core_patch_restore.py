from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcd_agent.mautic_core_restore import restore_retired_mcd_core_patches
from mcd_agent.models import MauticInstall


class RetiredCorePatchRestoreTests(unittest.TestCase):
    def _install(self, root: Path) -> MauticInstall:
        return MauticInstall(instance_uid="i", name="i", root=str(root), console_path=str(root / "bin/console"))

    def test_restores_retired_pagehit_patch_from_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app/bundles/PageBundle/Model/PageModel.php"
            path.parent.mkdir(parents=True)
            path.write_text("<?php\n// mcd pagehit cascade patch begin\npatched\n", encoding="utf-8")
            path.with_name(path.name + ".mcd-bak").write_text("<?php\noriginal\n", encoding="utf-8")

            result = restore_retired_mcd_core_patches(self._install(root))

            self.assertEqual(result["status"], "restored")
            self.assertEqual(path.read_text(encoding="utf-8"), "<?php\noriginal\n")

    def test_restores_retired_mautic7_campaign_timezone_patch_from_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app/bundles/CampaignBundle/Controller/EventController.php"
            path.parent.mkdir(parents=True)
            path.write_text("<?php\nDateTimeHelper::FORMAT_DB, 'local'\n", encoding="utf-8")
            path.with_name(path.name + ".mcd-campaign-tz-bak").write_text("<?php\noriginal\n", encoding="utf-8")

            result = restore_retired_mcd_core_patches(self._install(root))

            self.assertEqual(result["status"], "restored")
            self.assertEqual(path.read_text(encoding="utf-8"), "<?php\noriginal\n")

    def test_does_not_touch_clean_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app/bundles/PageBundle/Model/PageModel.php"
            path.parent.mkdir(parents=True)
            path.write_text("<?php\nclean\n", encoding="utf-8")

            result = restore_retired_mcd_core_patches(self._install(root))

            self.assertEqual(result["status"], "clean")
            self.assertEqual(path.read_text(encoding="utf-8"), "<?php\nclean\n")


if __name__ == "__main__":
    unittest.main()
