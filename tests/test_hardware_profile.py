from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcd_agent.hardware_profile import (
    capture_profile_authority,
    profile_selection_path,
    read_profile_selection,
    recommended_farm_profile,
    recommended_profile,
    reconcile_hardware_profile,
    restore_profile_authority,
    write_profile_selection,
)
from mcd_agent.mode import _read_profile_name
from mcd_agent.hardware_profile import _read_hardware_profile_name


class HardwareProfileTests(unittest.TestCase):
    def test_profile_selection_path_is_published(self) -> None:
        self.assertEqual(
            profile_selection_path("/srv/mcd"),
            Path("/srv/mcd/var/hardware-profile-selection.json"),
        )

    def test_recommendation_uses_conservative_cpu_and_memory_class(self) -> None:
        gib = 1024 * 1024
        self.assertEqual(recommended_profile(cpu_count=1, memory_kib=64 * gib), "tiny")
        self.assertEqual(recommended_profile(cpu_count=2, memory_kib=2 * gib), "tiny")
        self.assertEqual(recommended_profile(cpu_count=2, memory_kib=4 * gib), "mini")
        self.assertEqual(recommended_profile(cpu_count=4, memory_kib=8 * gib), "midi")
        self.assertEqual(recommended_profile(cpu_count=8, memory_kib=16 * gib), "maxi")
        self.assertEqual(recommended_profile(cpu_count=16, memory_kib=32 * gib), "hiload")
        self.assertEqual(recommended_profile(cpu_count=32, memory_kib=128 * gib), "ultra")
        self.assertEqual(recommended_profile(cpu_count=16, memory_kib=8 * gib), "midi")
        self.assertEqual(recommended_profile(cpu_count=2, memory_kib=3_911_572), "mini")

    def test_farm_recommendation_uses_separate_hardware_line(self) -> None:
        gib = 1024 * 1024
        self.assertEqual(recommended_farm_profile(cpu_count=1, memory_kib=1 * gib), "farm-tiny")
        self.assertEqual(recommended_farm_profile(cpu_count=2, memory_kib=4 * gib), "farm-mini")
        self.assertEqual(recommended_farm_profile(cpu_count=4, memory_kib=8 * gib), "farm-midi")
        self.assertEqual(recommended_farm_profile(cpu_count=8, memory_kib=16 * gib), "farm-maxi")
        self.assertEqual(recommended_farm_profile(cpu_count=12, memory_kib=64 * gib), "farm-hiload")
        self.assertEqual(recommended_farm_profile(cpu_count=32, memory_kib=128 * gib), "farm-ultra")

    def test_legacy_farm_is_forced_to_hardware_class_even_when_manual(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = root / "install"
            config = root / "mcd.toml"
            config.write_text('[profile]\nname = "midi"\n[hardware]\nprofile = "farm"\n', encoding="utf-8")
            write_profile_selection(
                str(install),
                mode="manual",
                profile="farm",
                cpu_count=4,
                memory_kib=8 * 1024 * 1024,
            )

            result = reconcile_hardware_profile(
                config_path=str(config),
                install_dir=str(install),
                cpu_count=4,
                memory_kib=8 * 1024 * 1024,
            )

            self.assertTrue(result.changed)
            self.assertEqual(result.profile, "farm-midi")
            self.assertEqual(_read_profile_name(str(config)), "midi")
            self.assertEqual(_read_hardware_profile_name(str(config)), "farm-midi")
            self.assertEqual(read_profile_selection(str(install))["profile"], "farm-midi")
            self.assertEqual(read_profile_selection(str(install))["mode"], "manual")

    def test_fresh_passive_install_is_automatically_profiled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "mcd.toml"
            config.write_text(
                '[profile]\nname = "passive"\n\n[hardware]\nprofile = "auto"\n\n[runtime]\ncampaign_total_parallel = 99\n',
                encoding="utf-8",
            )

            result = reconcile_hardware_profile(
                config_path=str(config),
                install_dir=str(root / "install"),
                cpu_count=2,
                memory_kib=4 * 1024 * 1024,
            )

            self.assertTrue(result.changed)
            self.assertEqual(result.mode, "auto")
            self.assertEqual(result.profile, "mini")
            self.assertEqual(_read_profile_name(str(config)), "passive")
            self.assertEqual(_read_hardware_profile_name(str(config)), "mini")
            self.assertEqual(read_profile_selection(str(root / "install"))["mode"], "auto")
            self.assertNotIn("campaign_total_parallel", config.read_text(encoding="utf-8"))

    def test_legacy_active_profile_is_preserved_as_manual(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "mcd.toml"
            config.write_text('[profile]\nname = "tiny"\n[hardware]\nprofile = "mini"\n', encoding="utf-8")

            result = reconcile_hardware_profile(
                config_path=str(config),
                install_dir=str(root / "install"),
                cpu_count=8,
                memory_kib=16 * 1024 * 1024,
            )

            self.assertFalse(result.changed)
            self.assertEqual(result.mode, "manual")
            self.assertEqual(_read_profile_name(str(config)), "tiny")
            self.assertEqual(_read_hardware_profile_name(str(config)), "mini")
            self.assertEqual(read_profile_selection(str(root / "install"))["mode"], "manual")

    def test_manual_mode_never_changes_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = root / "install"
            config = root / "mcd.toml"
            config.write_text('[profile]\nname = "midi"\n[hardware]\nprofile = "mini"\n', encoding="utf-8")
            write_profile_selection(str(install), mode="manual", profile="mini", cpu_count=16, memory_kib=0)

            result = reconcile_hardware_profile(
                config_path=str(config),
                install_dir=str(install),
                cpu_count=16,
                memory_kib=32 * 1024 * 1024,
            )

            self.assertFalse(result.changed)
            self.assertEqual(result.mode, "manual")
            self.assertEqual(_read_profile_name(str(config)), "midi")
            self.assertEqual(_read_hardware_profile_name(str(config)), "mini")

    def test_direct_edit_while_auto_locks_manual_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = root / "install"
            config = root / "mcd.toml"
            config.write_text('[profile]\nname = "midi"\n[hardware]\nprofile = "midi"\n', encoding="utf-8")
            write_profile_selection(str(install), mode="auto", profile="mini", cpu_count=2, memory_kib=0)

            result = reconcile_hardware_profile(
                config_path=str(config),
                install_dir=str(install),
                cpu_count=8,
                memory_kib=16 * 1024 * 1024,
            )

            self.assertFalse(result.changed)
            self.assertEqual(result.mode, "manual")
            self.assertEqual(_read_profile_name(str(config)), "midi")
            self.assertEqual(_read_hardware_profile_name(str(config)), "midi")
            self.assertEqual(read_profile_selection(str(install))["mode"], "manual")

    def test_auto_mode_tracks_a_hardware_class_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = root / "install"
            config = root / "mcd.toml"
            config.write_text('[profile]\nname = "midi"\n[hardware]\nprofile = "mini"\n', encoding="utf-8")
            write_profile_selection(str(install), mode="auto", profile="mini", cpu_count=2, memory_kib=0)

            result = reconcile_hardware_profile(
                config_path=str(config),
                install_dir=str(install),
                cpu_count=4,
                memory_kib=8 * 1024 * 1024,
            )

            self.assertTrue(result.changed)
            self.assertEqual(result.mode, "auto")
            self.assertEqual(_read_profile_name(str(config)), "midi")
            self.assertEqual(_read_hardware_profile_name(str(config)), "midi")
            self.assertEqual(read_profile_selection(str(install))["profile"], "midi")

    def test_rollout_and_rollback_restore_authoritative_profile_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = root / "install"
            config = root / "mcd.toml"
            config.write_text(
                "\n".join(
                    [
                        "[profile]",
                        'name = "farm-hiload"',
                        "",
                        "[hardware]",
                        'profile = "farm-hiload"',
                        "",
                        "[runtime]",
                        "segment_retry_count = 3",
                        "other_value = 7",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            write_profile_selection(
                str(install),
                mode="manual",
                profile="farm-hiload",
                cpu_count=12,
                memory_kib=64 * 1024 * 1024,
            )
            snapshot = capture_profile_authority(str(config), str(install))

            config.write_text(
                "\n".join(
                    [
                        "[profile]",
                        'name = "passive"',
                        "",
                        "[hardware]",
                        'profile = "auto"',
                        "",
                        "[runtime]",
                        "segment_retry_count = 0",
                        "other_value = 99",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            write_profile_selection(
                str(install),
                mode="auto",
                profile="passive",
                cpu_count=1,
                memory_kib=1,
            )
            restore_profile_authority(snapshot)

            self.assertEqual(_read_profile_name(str(config)), "farm-hiload")
            self.assertEqual(_read_hardware_profile_name(str(config)), "farm-hiload")
            self.assertIn("segment_retry_count = 0", config.read_text(encoding="utf-8"))
            selection = read_profile_selection(str(install))
            self.assertEqual(selection["mode"], "manual")
            self.assertEqual(selection["profile"], "farm-hiload")


if __name__ == "__main__":
    unittest.main()
