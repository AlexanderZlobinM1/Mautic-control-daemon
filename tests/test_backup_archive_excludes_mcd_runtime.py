from pathlib import Path
from types import SimpleNamespace
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

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
    def test_file_list_race_does_not_fail_backup_archive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "instance"
            root.mkdir()
            (root / "index.php").write_text("ok\n", encoding="utf-8")
            transient = root / "rotating.bak"
            transient.write_text("temporary\n", encoding="utf-8")
            out = Path(td) / "backup"
            out.mkdir()
            cfg = SimpleNamespace(
                backup_archive_enabled=True,
                backup_archive_paths=[str(root)],
                backup_archive_name="files.tar.gz",
                backup_dump_timeout_sec=30,
            )

            original_run = __import__("mcd_agent.backup", fromlist=["_run"])._run

            def delete_before_tar(command: list[str], **kwargs: object):
                transient.unlink(missing_ok=True)
                return original_run(command, **kwargs)

            with patch("mcd_agent.backup._run", side_effect=delete_before_tar):
                _archive_files(cfg, out)

            names = _archive_member_names(out / "files.tar.gz")
            self.assertTrue(any(name.endswith("/index.php") for name in names), names)
            self.assertFalse(any(name.endswith("/rotating.bak") for name in names), names)

    def test_host_file_archive_excludes_mcd_runtime_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "var" / "www" / "site" / "public_html"
            root.mkdir(parents=True)
            (root / "index.php").write_text("ok\n", encoding="utf-8")
            (root / "var" / "logs").mkdir(parents=True)
            (root / "var" / "logs" / "rolling.log").write_text("runtime log\n", encoding="utf-8")
            (root / "var" / "cache").mkdir(parents=True)
            (root / "var" / "cache" / "container.php").write_text("runtime cache\n", encoding="utf-8")
            (root / "var" / "spool").mkdir(parents=True)
            (root / "var" / "spool" / "message.pending").write_text("queue state\n", encoding="utf-8")
            (root / "app" / "logs").mkdir(parents=True)
            (root / "app" / "logs" / "legacy.log").write_text("legacy runtime log\n", encoding="utf-8")
            for directory, filename in (
                ("var/queue", "pending.json"),
                ("var/tmp", "temporary.dat"),
                ("var/sessions", "session.data"),
                ("app/cache", "container.php"),
            ):
                runtime = root / directory
                runtime.mkdir(parents=True, exist_ok=True)
                (runtime / filename).write_text("runtime state\n", encoding="utf-8")
            persistent = root / "docroot" / "media" / "images" / "retained.png"
            persistent.parent.mkdir(parents=True)
            persistent.write_bytes(b"persistent upload")
            runtime_dir = root / "var" / "logs"
            runtime_dir.chmod(0o750)
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

            stop_writer = threading.Event()
            def update_live_log() -> None:
                log = runtime_dir / "rolling.log"
                while not stop_writer.is_set():
                    with log.open("a", encoding="utf-8") as stream:
                        stream.write("continuously changing runtime log\n")

            writer = threading.Thread(target=update_live_log)
            writer.start()
            try:
                _archive_files(cfg, out)
            finally:
                stop_writer.set()
                writer.join(timeout=5)

            names = _archive_member_names(out / "files.tar.gz")
            self.assertFalse(_contains_mcd_runtime(names), names)
            self.assertTrue(any(name.endswith("index.php") for name in names), names)
            self.assertTrue(any(name.endswith("retained.png") for name in names), names)
            for excluded in ("rolling.log", "container.php", "message.pending", "legacy.log", "pending.json", "temporary.dat", "session.data"):
                self.assertFalse(any(name.endswith(excluded) for name in names), names)
            for directory in ("var/logs", "var/cache", "var/spool", "var/queue", "var/tmp", "var/sessions", "app/logs", "app/cache"):
                self.assertTrue(any(name.endswith("/" + directory) for name in names), (directory, names))
            restored = Path(td) / "restored"
            restored.mkdir()
            import subprocess
            subprocess.run(["tar", "-xzf", str(out / "files.tar.gz"), "-C", str(restored)], check=True)
            restored_runtime_dir = restored / str(root).lstrip("/") / "var" / "logs"
            self.assertTrue(restored_runtime_dir.is_dir())
            self.assertEqual(restored_runtime_dir.stat().st_mode & 0o777, 0o750)
            self.assertEqual(restored_runtime_dir.stat().st_uid, runtime_dir.stat().st_uid)
            self.assertEqual(restored_runtime_dir.stat().st_gid, runtime_dir.stat().st_gid)

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
            self.assertIn("index.php", names)
