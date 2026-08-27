from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent import mautic_version_cache


class MauticVersionCacheTest(unittest.TestCase):
    def test_candidate_roots_never_escape_to_shared_var_www(self) -> None:
        roots = mautic_version_cache._candidate_roots("/var/www/client/public_html")

        self.assertEqual(
            roots,
            [Path("/var/www/client/public_html"), Path("/var/www/client")],
        )

    def test_cached_version_with_wrong_major_is_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site" / "public_html"
            root.mkdir(parents=True)
            console = root / "bin" / "console"
            console.parent.mkdir()
            console.write_text("<?php\n", encoding="utf-8")
            generated = Path(td) / "generated"

            with (
                patch.object(mautic_version_cache, "_VERSION_CACHE_ROOT", generated),
                patch.object(mautic_version_cache, "_read_version_from_mcd_source", return_value="6.0.9"),
            ):
                mautic_version_cache.write_mautic_version_cache(root, "7.1.3")
                actual = mautic_version_cache.collect_mautic_version(
                    str(root),
                    "/usr/bin/php",
                    console_path=str(console),
                    expected_major=6,
                )

                self.assertEqual(actual, "6.0.9")
                self.assertEqual(mautic_version_cache.read_cached_mautic_version(root), "6.0.9")

    def test_migrates_legacy_cache_outside_instance_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "var" / "www" / "site" / "public_html"
            legacy = root / ".mcd" / "mautic.version"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("6.0.9\n", encoding="utf-8")
            generated = Path(td) / "opt" / "mcd" / "generated" / "mautic-version"

            with patch.object(mautic_version_cache, "_VERSION_CACHE_ROOT", generated):
                self.assertTrue(mautic_version_cache.migrate_legacy_mautic_version_cache(root))
                self.assertEqual(mautic_version_cache.read_cached_mautic_version(root), "6.0.9")
                self.assertTrue(mautic_version_cache.version_cache_path(root).is_file())

            self.assertEqual(legacy.read_text(encoding="utf-8"), "6.0.9\n")


if __name__ == "__main__":
    unittest.main()
