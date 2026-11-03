from __future__ import annotations

from importlib import metadata
import json
import re
from pathlib import Path
from typing import Any

from mcd_agent import __version__

_VERSION_RE = re.compile(r"__version__\s*=\s*['\"]([^'\"]+)['\"]")
_SOURCE_REVISION_RE = re.compile(r"[0-9a-f]{7,64}")


def source_version(source_dir: Path | str | None = None) -> str | None:
    # Source inspection is an explicit development/diagnostic operation only.
    # Installed runtime identity must never discover a parallel project checkout.
    if source_dir is None:
        return None
    src = Path(source_dir)
    init_py = src / "mcd_agent" / "__init__.py"
    try:
        text = init_py.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        manifest = src / "mcd-runtime-manifest.json"
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            version = str(raw.get("version") or "").strip() if isinstance(raw, dict) else ""
            return version or None
        except Exception:
            return None
    match = _VERSION_RE.search(text)
    if not match:
        manifest = src / "mcd-runtime-manifest.json"
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            version = str(raw.get("version") or "").strip() if isinstance(raw, dict) else ""
            return version or None
        except Exception:
            return None
    version = match.group(1).strip()
    return version or None


def installed_agent_version(source_dir: Path | str | None = None) -> str:
    # The imported runtime is authoritative, including when an old checkout exists.
    return __version__


def package_agent_version(distribution: str = "mcd-agent") -> str:
    try:
        return str(metadata.version(distribution) or "").strip()
    except metadata.PackageNotFoundError:
        return ""
    except Exception:
        return ""


def runtime_source_revision(runtime_manifest: Path | str | None = None) -> str | None:
    """Return the immutable source commit bound by an installed runtime manifest.

    Discovery is deliberately explicit. Installed identity must never search a
    parallel checkout or guess an installation root; callers derive the manifest
    path from their configured installation entrypoint.
    """
    if runtime_manifest is None:
        return None
    try:
        raw = json.loads(Path(runtime_manifest).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict) or raw.get("schema") != "mcd-runtime-package-v1":
        return None
    revision = str(raw.get("source_commit") or "").strip().lower()
    return revision if _SOURCE_REVISION_RE.fullmatch(revision) else None


def agent_version_payload(
    source_dir: Path | str | None = None,
    *,
    runtime_manifest: Path | str | None = None,
) -> dict[str, Any]:
    installed = installed_agent_version(source_dir)
    source = source_version(source_dir) or ""
    source_revision = runtime_source_revision(runtime_manifest) or ""
    package = package_agent_version()
    running = __version__
    return {
        # Legacy field consumed by MCC and older tooling. Keep it tied to the
        # actually running package; the source tree can be stale or staged.
        "agent_version": running,
        "agent_running_version": running,
        "agent_installed_version": installed,
        "agent_source_version": source,
        "agent_source_revision": source_revision,
        "agent_package_version": package,
        "agent_package_mismatch": bool(package and package != running),
        "agent_version_mismatch": bool((source and source != running) or (package and package != running)),
    }
