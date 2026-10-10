"""Keep the managed MCD venv package ahead of a stale source-tree install."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def _source_root(path: str) -> Path | None:
    try:
        root = Path(path or os.getcwd()).resolve()
    except Exception:
        return None
    if root.name != "src":
        return None
    if not (root / "pyproject.toml").is_file():
        return None
    if not (root / "mcd_agent" / "__init__.py").is_file():
        return None
    return root


def _managed_site_dir() -> Path | None:
    for raw in list(sys.path):
        try:
            candidate = Path(raw or os.getcwd()).resolve()
        except Exception:
            continue
        if not (candidate / "mcd_agent" / "__init__.py").is_file():
            continue
        if not any(candidate.glob("mcd_agent-*.dist-info")):
            continue
        return candidate
    return None


def _remove_shadow_hooks(site_dir: Path, source_roots: set[Path]) -> None:
    source_text = {str(root) for root in source_roots}
    for candidate in sorted(site_dir.iterdir()):
        name = candidate.name.lower()
        is_agent_hook = name in {"mcd_agent.egg-link", "mcd-agent.egg-link"}
        is_agent_hook = is_agent_hook or (
            name.startswith("__editable__")
            and ("mcd_agent" in name or "mcd-agent" in name)
        )
        if candidate.suffix.lower() == ".pth" and candidate.is_file():
            try:
                content = candidate.read_text(encoding="utf-8")
            except OSError:
                content = ""
            if any(root in content for root in source_text):
                lines = [line for line in content.splitlines(keepends=True) if line.strip() not in source_text]
                if lines:
                    candidate.write_text("".join(lines), encoding="utf-8")
                    continue
                is_agent_hook = True
        if not is_agent_hook:
            continue
        try:
            if candidate.is_dir() and not candidate.is_symlink():
                for child in candidate.iterdir():
                    if child.is_file() or child.is_symlink():
                        child.unlink(missing_ok=True)
                candidate.rmdir()
            else:
                candidate.unlink(missing_ok=True)
        except OSError:
            # The updater's installed-path verification remains fail-closed if
            # a hook cannot be removed; startup must not be broken by cleanup.
            continue


def _repair_mcd_source_shadow() -> None:
    site_dir = _managed_site_dir()
    if site_dir is None:
        return

    source_roots = {
        root
        for raw in list(sys.path)
        if (root := _source_root(raw)) is not None
    }
    if source_roots:
        sys.path[:] = [
            raw
            for raw in sys.path
            if (_source_root(raw) is None or _source_root(raw) not in source_roots)
        ]

    sys.meta_path[:] = [
        finder
        for finder in sys.meta_path
        if not (
            "__editable__" in f"{type(finder).__module__}.{type(finder).__name__}".lower()
            and "mcd_agent" in f"{type(finder).__module__}.{type(finder).__name__}".lower()
        )
    ]
    _remove_shadow_hooks(site_dir, source_roots)


try:
    _repair_mcd_source_shadow()
except Exception:
    # This is only a compatibility bootstrap. Never make an unrelated Python
    # invocation fail; the normal updater verification still fails closed.
    pass
