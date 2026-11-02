#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


REQUIRED_TESTS = (
    "test_mode_cron_wrappers.py",
    "test_mautic_version_cache.py",
    "test_mautic7_database_preflight.py",
    "test_mautic_upgrade_contract.py",
    "test_mautic_composer_move.py",
    "test_production_runtime_boundary.py",
)


def validate(root: Path) -> None:
    root = root.resolve()
    tests_dir = root / "tests"
    for name in REQUIRED_TESTS:
        if not (tests_dir / name).is_file():
            raise RuntimeError(f"required release regression is missing: tests/{name}")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)
    command = [
        sys.executable,
        "-m",
        "unittest",
        *(str(tests_dir / name) for name in REQUIRED_TESTS),
    ]
    proc = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "release regression failed").strip()
        raise RuntimeError(detail)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    try:
        validate(Path(args.root))
    except RuntimeError as exc:
        print(f"release regression failed: {exc}", file=sys.stderr)
        return 1
    print("release regression passed: DB upgrade gate and cron/version matrix")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
