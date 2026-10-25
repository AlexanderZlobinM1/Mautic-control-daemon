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
import ast
import base64
import hashlib
import json
from pathlib import Path
import re
from typing import Any


SCHEMA = "mcd-profile-authority-probe-v1"
PROFILE_SELECTION_FILE = "hardware-profile-selection.json"


class ProbeError(RuntimeError):
    """The pre-deploy authority snapshot cannot be proven complete."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strip_toml_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = ""
        elif quote == "'":
            if char == "'":
                quote = ""
        elif char in {'"', "'"}:
            quote = char
        elif char == "#":
            return line[:index]
    if quote:
        raise ProbeError("unterminated string while reading include.files")
    return line


def _array_depth(value: str) -> int:
    depth = 0
    quote = ""
    escaped = False
    for char in value:
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = ""
        elif quote == "'":
            if char == "'":
                quote = ""
        elif char in {'"', "'"}:
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth < 0:
                raise ProbeError("malformed include.files array")
    if quote:
        raise ProbeError("unterminated string in include.files array")
    return depth


def _include_files(config: Path) -> list[Path]:
    try:
        text = config.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProbeError(f"cannot read declared config file {config}: {exc}") from exc

    section = ""
    value_parts: list[str] | None = None
    values: list[str] | None = None
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        clean = _strip_toml_comment(lines[index]).strip()
        index += 1
        if not clean:
            continue
        if clean.startswith("["):
            # Included system config is a complete TOML document and may
            # contain array-of-table declarations (for example [[jobs]] and
            # [[instances]]).  The probe only needs to locate the include
            # table, but it must still accept every valid section form so a
            # real split config cannot be rejected before the read-only
            # authority snapshot is captured.
            match = re.fullmatch(
                r"(?:\[\s*([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\s*\]|"
                r"\[\[\s*([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\s*\]\])",
                clean,
            )
            if not match:
                raise ProbeError(f"malformed TOML section in {config}")
            section = match.group(1) or match.group(2)
            continue
        if section != "include":
            continue
        if "=" not in clean:
            raise ProbeError(f"malformed include declaration in {config}")
        key, first_value = clean.split("=", 1)
        if key.strip() != "files":
            continue
        if value_parts is not None:
            raise ProbeError(f"duplicate include.files declaration in {config}")
        value_parts = [first_value.strip()]
        depth = _array_depth(value_parts[0])
        while depth > 0 and index < len(lines):
            part = _strip_toml_comment(lines[index]).strip()
            index += 1
            value_parts.append(part)
            depth = _array_depth("\n".join(value_parts))
        if depth != 0:
            raise ProbeError(f"unterminated include.files array in {config}")
        try:
            parsed = ast.literal_eval("\n".join(value_parts))
        except (SyntaxError, ValueError) as exc:
            raise ProbeError(f"malformed include.files value in {config}") from exc
        if not isinstance(parsed, list) or any(not isinstance(item, str) or not item.strip() for item in parsed):
            raise ProbeError(f"include.files must be a non-empty-string array in {config}")
        values = [item.strip() for item in parsed]

    if value_parts is None:
        return []
    return [
        (config.parent / item).resolve() if not Path(item).is_absolute() else Path(item).resolve()
        for item in values or []
    ]


def _config_files(config: Path) -> list[dict[str, Any]]:
    if not config.is_file():
        raise ProbeError(f"declared config file is missing: {config}")
    files: list[dict[str, Any]] = []
    seen: set[Path] = set()

    def visit(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        files.append({"path": str(resolved), "exists": resolved.is_file()})
        for child in _include_files(resolved):
            if not child.is_file():
                raise ProbeError(f"declared include file is missing: {child}")
            visit(child)

    visit(config)
    return files


def _mutable_config_path(config: Path, files: list[dict[str, Any]]) -> Path:
    direct = _include_files(config)
    existing = [path for path in direct if path.is_file()]
    return existing[-1] if existing else config


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
    try:
        payload = capture(install_dir=install_dir, config=config)
    except ProbeError as exc:
        print(
            json.dumps(
                {"schema": SCHEMA, "status": "error", "read_only": True, "reason": str(exc)},
                ensure_ascii=True,
            )
        )
        return 1
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

