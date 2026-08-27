from pathlib import Path
from types import SimpleNamespace
import tarfile
import tempfile
import unittest

from mcd_agent.backup import _archive_files, _archive_instance_files
from mcd_agent.models import MauticInstall


def _archive_member_names(path: Path) -> list[str]:
    with tarfile.open(path, "r:gz") as tf:
        return tf.getnames()


def _contains_mcd_runtime(names: list[str]) -> bool:
    normalized = [str(x).strip("/") for x in names]
    return any(
        x == ".mcd"
        or x.startswith(".mcd/")
        or "/.mcd" in x
        or "/.mcd/" in x
        or x == "./.mcd"
        or x.startswith("./.mcd/")
        for x in normalized
    )


class BackupArchiveExcludesMcdRuntimeTest(unittest.TestCase):
    def test_host_file_archive_excludes_mcd_runtime_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "var" / "www" / "site" / "public_html"
            root.mkdir(parents=True)
            (root / "index.php").write_text("ok\n", encoding="utf-8")
            (root / ".mcd").mkdir()
            (root / ".mcd" / "mautic.version").write_text("6.0.9\n", encoding="utf-8")
            out = Path(td) / "backup"
            out.mkdir()
            cfg = SimpleNamespace(
                backup_archive_enabled=True,
                backup_archive_paths=[str(root)],
                backup_archive_name="files.tar.gz",
                backup_dump_timeout_sec=30,
            )

            _archive_files(cfg, out)

            names = _archive_member_names(out / "files.tar.gz")
            self.assertFalse(_contains_mcd_runtime(names), names)
            self.assertTrue(any(name.endswith("index.php") for name in names), names)

    def test_instance_file_archive_excludes_mcd_runtime_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "public_html"
            root.mkdir()
            (root / "index.php").write_text("ok\n", encoding="utf-8")
            (root / ".mcd").mkdir()
            (root / ".mcd" / "php").write_text("generated\n", encoding="utf-8")
            out = Path(td) / "backup"
            out.mkdir()
            cfg = SimpleNamespace(backup_dump_timeout_sec=30)
            inst = MauticInstall(
                instance_uid="site",
                name="site.example",
                root=str(root),
                console_path=str(root / "bin" / "console"),
            )

            _archive_instance_files(cfg, inst, out)

            names = _archive_member_names(out / "files.tar.gz")
            self.assertFalse(_contains_mcd_runtime(names), names)
            self.assertIn("./index.php", names)

