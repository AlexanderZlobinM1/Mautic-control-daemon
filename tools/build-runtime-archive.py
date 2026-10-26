#!/usr/bin/env python3
"""Build the detached, runtime-only MCD self-update archive.

The source checkout remains the input to the release gate, but it must never
become the production runtime payload.  The resulting archive contains only
the importable agent package, runtime resources, dependency requirements, a
wheel built from the exact source, and a manifest whose payload hashes do not
include the manifest itself.  The archive hash and source archive hash belong
to the external release receipt, so the manifest is not self-referential.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone


MANIFEST_NAME = "mcd-runtime-manifest.json"
MANIFEST_SCHEMA = "mcd-runtime-package-v1"
REQUIRED_RUNTIME_FILES = {
    "mcd_agent/__init__.py",
    "mcd_agent/daemon.py",
    "mcd_agent/state_push.py",
    "mcd_agent/mcd_control.py",
}
FORBIDDEN_TOP_LEVEL = {
    ".git",
    ".pytest_cache",
    "ALGORITHM.md",
    "CHANGELOG.md",
    "README.md",
    "TECHNICAL_DEBT.md",
    "TEST_ALGORITHM.md",
    "docs",
    "tests",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_version(source: Path) -> str:
    init = source / "mcd_agent" / "__init__.py"
    text = init.read_text(encoding="utf-8")
    marker = '__version__ = "'
    start = text.find(marker)
    if start < 0:
        raise RuntimeError(f"cannot find __version__ in {init}")
    start += len(marker)
    end = text.find('"', start)
    if end <= start:
        raise RuntimeError(f"cannot parse __version__ in {init}")
    return text[start:end]


def _git_commit(source: Path, ref: str) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=str(source),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git revision lookup failed").strip()
        raise RuntimeError(f"cannot resolve canonical source commit {ref}: {detail}")
    commit = (proc.stdout or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise RuntimeError(f"git revision lookup returned invalid commit for {ref}: {commit!r}")
    return commit.lower()


def _canonical_source_commit(source: Path, requested: str) -> str:
    head = _git_commit(source, "HEAD")
    canonical = _git_commit(source, "origin/main")
    if head != canonical:
        raise RuntimeError(
            f"source checkout is not canonical origin/main: HEAD={head} origin/main={canonical}"
        )
    requested = str(requested or "").strip().lower()
    if requested and requested != canonical:
        raise RuntimeError(
            f"source commit mismatch: requested={requested} canonical_origin_main={canonical}"
        )
    return canonical


def _copy_runtime_resources(source: Path, stage: Path) -> None:
    package = source / "mcd_agent"
    requirements = source / "requirements.txt"
    if not package.is_dir():
        raise RuntimeError(f"runtime package is missing: {package}")
    if not requirements.is_file():
        raise RuntimeError(f"runtime requirements are missing: {requirements}")

    shutil.copytree(package, stage / "mcd_agent", symlinks=True, ignore=_ignore_runtime_artifacts)
    shutil.copy2(requirements, stage / "requirements.txt")
    missing = sorted(
        relative
        for relative in REQUIRED_RUNTIME_FILES
        if not (stage / relative).is_file()
    )
    if missing:
        raise RuntimeError(f"runtime package is missing required import files: {missing}")

    etc_src = source / "etc"
    if etc_src.is_dir():
        etc_dst = stage / "etc"
        etc_dst.mkdir()
        for item in sorted(etc_src.iterdir()):
            if item.is_file() and item.name.startswith("mcd-agent") and item.suffix == ".toml":
                shutil.copy2(item, etc_dst / item.name)


def _ignore_runtime_artifacts(_path: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in {"__pycache__", ".pytest_cache", ".mypy_cache"}
        or name.endswith(".pyc")
        or name.startswith("._")
        or name == ".DS_Store"
    }


def _build_wheel(source: Path, wheel_dir: Path) -> Path:
    wheel_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-input",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
            str(source),
        ],
        cwd=str(source),
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "pip wheel failed").strip()
        raise RuntimeError(f"runtime wheel build failed: {detail}")
    wheels = sorted(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"runtime wheel build produced {len(wheels)} wheels")
    return wheels[0]


def _normalise_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def _write_deterministic_archive(stage: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as raw:
        import gzip

        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path in sorted(stage.rglob("*")):
                    archive.add(
                        path,
                        arcname=path.relative_to(stage).as_posix(),
                        recursive=False,
                        filter=_normalise_tarinfo,
                    )


def build(source: Path, destination: Path, *, source_commit: str = "", source_archive_sha256: str = "") -> Path:
    source = source.resolve()
    source_commit = _canonical_source_commit(source, source_commit)
    version = _source_version(source)
    manifest_sha256 = ""
    with tempfile.TemporaryDirectory(prefix="mcd-runtime-build-") as tmp:
        tmp_path = Path(tmp)
        wheel_dir = tmp_path / "wheelhouse"
        wheel = _build_wheel(source, wheel_dir)
        stage = tmp_path / "stage"
        stage.mkdir()
        _copy_runtime_resources(source, stage)
        wheel_stage = stage / "wheel"
        wheel_stage.mkdir()
        staged_wheel = wheel_stage / wheel.name
        shutil.copy2(wheel, staged_wheel)

        payload: dict[str, str] = {}
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                payload[path.relative_to(stage).as_posix()] = _sha256(path)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "version": version,
            "source_commit": source_commit,
            "wheel": staged_wheel.relative_to(stage).as_posix(),
            "requirements": "requirements.txt",
            "payload_sha256": payload,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest_path = stage / MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_sha256 = _sha256(manifest_path)

        forbidden = [
            str(path.relative_to(stage))
            for path in stage.rglob("*")
            if path.name in FORBIDDEN_TOP_LEVEL or path.parts and path.parts[0] in {"tests", "docs"}
        ]
        if forbidden:
            raise RuntimeError(f"runtime archive contains forbidden source material: {forbidden}")
        _write_deterministic_archive(stage, destination)

    receipt_path = destination.with_suffix(destination.suffix + ".receipt.json")
    receipt = {
        "schema": "mcd-runtime-package-receipt-v1",
        "version": version,
        "archive": destination.name,
        "archive_sha256": _sha256(destination),
        "manifest_sha256": manifest_sha256,
        "source_commit": source_commit,
        "source_archive_sha256": source_archive_sha256,
        "runtime_only": True,
        "forbidden_source_material": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt_path


def main() -> int:
    if sys.version_info < (3, 10):
        raise RuntimeError("runtime archive build requires Python 3.10+ (the MCD runtime requirement)")
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", default="")
    parser.add_argument("--source-archive-sha256", default="")
    args = parser.parse_args()
    receipt = build(
        args.source,
        args.output,
        source_commit=args.source_commit,
        source_archive_sha256=args.source_archive_sha256,
    )
    print(receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
