from __future__ import annotations

from importlib import metadata
import re
from pathlib import Path
from typing import Any

from mcd_agent import __version__

_VERSION_RE = re.compile(r"__version__\s*=\s*['\"]([^'\"]+)['\"]")


def source_version(source_dir: Path | str | None = None) -> str | None:
    src = Path(source_dir) if source_dir is not None else Path("/opt/mcd/src")
    init_py = src / "mcd_agent" / "__init__.py"
    try:
        text = init_py.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    match = _VERSION_RE.search(text)
    if not match:
        return None
    version = match.group(1).strip()
    return version or None


def installed_agent_version(source_dir: Path | str | None = None) -> str:
    return source_version(source_dir) or __version__


def package_agent_version(distribution: str = "mcd-agent") -> str:
    try:
        return str(metadata.version(distribution) or "").strip()
    except metadata.PackageNotFoundError:
        return ""
    except Exception:
        return ""


def agent_version_payload(source_dir: Path | str | None = None) -> dict[str, Any]:
    installed = installed_agent_version(source_dir)
    source = source_version(source_dir) or ""
    package = package_agent_version()
    running = __version__
    return {
        # Legacy field consumed by MCC and older tooling. Keep it tied to the
        # actually running package; the source tree can be stale or staged.
        "agent_version": running,
        "agent_running_version": running,
        "agent_installed_version": installed,
        "agent_source_version": source,
        "agent_package_version": package,
        "agent_package_mismatch": bool(source and package and source != package),
        "agent_version_mismatch": bool((source and source != running) or (source and package and source != package)),
    }
