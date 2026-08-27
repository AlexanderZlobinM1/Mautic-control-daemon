from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def _requirements_file() -> Path:
    # /opt/mcd/src/mcd_agent/__main__.py -> /opt/mcd/src/requirements.txt
    return Path(__file__).resolve().parent.parent / "requirements.txt"


def _bootstrap_requirements_on_missing_module(exc: ModuleNotFoundError) -> bool:
    missing = (getattr(exc, "name", "") or "").strip()
    if not missing:
        return False
    req = _requirements_file()
    if not req.exists():
        return False
    print(
        f"mcd bootstrap: missing module '{missing}', installing requirements from {req}",
        file=sys.stderr,
    )
    pip_probe = subprocess.run([sys.executable, "-m", "pip", "--version"], cwd="/", capture_output=True, text=True)
    if pip_probe.returncode != 0:
        ensure = subprocess.run(
            [sys.executable, "-m", "ensurepip", "--upgrade"],
            cwd="/",
            capture_output=True,
            text=True,
        )
        if ensure.returncode != 0:
            detail = (ensure.stderr or ensure.stdout or "ensurepip failed").strip()
            print(f"mcd bootstrap: pip bootstrap failed: {detail}", file=sys.stderr)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "-r",
        str(req),
    ]
    proc = subprocess.run(cmd, cwd="/", capture_output=True, text=True)
    if proc.returncode == 0:
        return True
    detail = (proc.stderr or proc.stdout or "pip install failed").strip()
    print(f"mcd bootstrap: dependency install failed: {detail}", file=sys.stderr)
    return False


def _load_main():
    try:
        from mcd_agent.cli import main  # type: ignore[import-not-found]
        return main
    except ModuleNotFoundError as exc:
        if not _bootstrap_requirements_on_missing_module(exc):
            raise
        from mcd_agent.cli import main  # type: ignore[import-not-found]
        return main


if __name__ == "__main__":
    raise SystemExit(_load_main()())
