#!/usr/bin/env python3
"""Read-only pre-deploy capture of MCD profile authority.

This sidecar is intentionally independent of the installed MCD runtime so it
can run against legacy 1.5.131/1.5.133 before a runtime-only bridge.  It reads
the entrypoint/include graph and the published hardware-selection state file;
it never imports mcd_agent, starts services, writes files, or installs a
package.  The exact sidecar file must be transported from a verified release
candidate and its SHA bound by the caller's release manifest.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 sidecar hosts
    import tomli as tomllib  # type: ignore[no-redef]


SCHEMA = "mcd-profile-authority-probe-v1"
PROFILE_SELECTION_FILE = "hardware-profile-selection.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _config_files(config: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    seen: set[Path] = set()

    def visit(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        files.append({"path": str(resolved), "exists": resolved.is_file()})
        if not resolved.is_file():
            return
        try:
            text = resolved.read_text(encoding="utf-8")
            if "MCD_CONFIG_ENTRYPOINT v1" not in text:
                return
            raw = tomllib.loads(text)
        except Exception:
            return
        include = raw.get("include", {}) if isinstance(raw, dict) else {}
        files_raw = include.get("files", []) if isinstance(include, dict) else []
        values = files_raw if isinstance(files_raw, list) else [files_raw]
        for item in values:
            value = str(item or "").strip()
            if not value:
                continue
            child = Path(value)
            if not child.is_absolute():
                child = resolved.parent / child
            visit(child)

    visit(config)
    return files


def _mutable_config_path(config: Path, files: list[dict[str, Any]]) -> Path:
    existing = [Path(str(row["path"])) for row in files if bool(row.get("exists"))]
    return existing[-1] if len(existing) > 1 else config


def capture(*, install_dir: Path, config: Path) -> dict[str, Any]:
    files = _config_files(config)
    selection_path = install_dir / "var" / PROFILE_SELECTION_FILE
    exists = selection_path.is_file()
    raw = selection_path.read_bytes() if exists else b""
    return {
        "schema": SCHEMA,
        "read_only": True,
        "capture_phase": "pre_runtime_install",
        "install_dir": str(install_dir),
        "config_path": str(config),
        "config_mutable_path": str(_mutable_config_path(config, files)),
        "config_files": files,
        "selection_path": str(selection_path),
        "selection_exists": exists,
        "selection_sha256": _sha256(selection_path) if exists else "",
        "selection_bytes_base64": base64.b64encode(raw).decode("ascii"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dir", default="/opt/mcd")
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true", help="Emit the JSON contract (the default output)")
    args = parser.parse_args(argv)
    install_dir = Path(args.install_dir).resolve()
    config = Path(args.config or (install_dir / "etc" / "mcd.toml")).resolve()
    print(json.dumps(capture(install_dir=install_dir, config=config), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

