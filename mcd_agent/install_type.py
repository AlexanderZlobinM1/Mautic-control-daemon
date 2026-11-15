from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from mcd_agent.runtime_descriptor import descriptor_for_root


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None
    if isinstance(data, dict):
        return data
    return None


def _is_recommended_project(proj_dir: Path) -> bool:
    data = _load_json(proj_dir / "composer.json")
    if not data:
        return False
    name = str(data.get("name", "")).strip().lower()
    req = data.get("require", {})
    req_keys = set(req.keys()) if isinstance(req, dict) else set()
    if name == "mautic/recommended-project":
        return True
    if "mautic/recommended-project" in req_keys:
        return True
    # Common marker in recommended-project installs.
    return "mautic/core-composer-scaffold" in req_keys


def detect_host_install_type(root: str) -> str:
    p = Path(root).resolve()
    parent = p.parent
    base = p.name.lower()

    # Layout and dependency files are also present in official ZIP releases.
    # Only explicit project ownership identifies the Composer update method.
    own_project = _is_recommended_project(p)
    parent_project = base in {"docroot", "public"} and _is_recommended_project(parent)
    if own_project and parent_project:
        return "unknown"
    if own_project or parent_project:
        return "composer"

    # Conservative fallback:
    # package/zip installs contain composer files too, so do not classify as composer
    # by composer.lock alone to avoid false positives.
    return "zip"


def detect_install_type(root: str) -> str:
    try:
        descriptor = descriptor_for_root(root)
    except (OSError, ValueError):
        return "unknown"
    if descriptor is not None:
        return str(descriptor.install_type or "unknown").strip().lower() or "unknown"
    return detect_host_install_type(root)


def plugin_dir_candidates(root: str | Path) -> list[Path]:
    base = Path(root)
    try:
        descriptor = descriptor_for_root(str(base))
    except (OSError, ValueError):
        return []
    if descriptor is not None:
        if not descriptor.has_capability("plugin-read"):
            return []
        return [descriptor.host_plugins_path] if descriptor.host_plugins_path is not None else []
    composer_layout = (
        detect_install_type(str(base)) == "composer"
        or (base / "docroot").is_dir()
        or (base / "public").is_dir()
    )
    if composer_layout:
        return [
            base / "docroot" / "plugins",
            base / "public" / "plugins",
            base / "plugins",
        ]
    return [
        base / "plugins",
        base / "docroot" / "plugins",
        base / "public" / "plugins",
    ]


def is_complete_plugin_bundle(plugin_dir: Path, bundle_name: str) -> bool:
    """Return whether a directory contains the minimum Mautic plugin shape.

    A directory can survive a partial or manual removal, but it is not an
    installed plugin unless both Mautic metadata and the bundle entry class are
    present. This intentionally inspects only plugin-owned files.
    """
    name = str(bundle_name or "").strip()
    if not name or not plugin_dir.is_dir():
        return False
    config_path = plugin_dir / "Config" / "config.php"
    entry_path = plugin_dir / f"{name}.php"
    if not config_path.is_file() or not entry_path.is_file():
        return False
    try:
        entry_source = entry_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return bool(re.search(rf"\bclass\s+{re.escape(name)}\b", entry_source))


def app_bundle_dir_candidates(root: str | Path) -> list[Path]:
    base = Path(root)
    try:
        descriptor = descriptor_for_root(str(base))
    except (OSError, ValueError):
        return []
    if descriptor is not None:
        return []
    composer_layout = (
        detect_install_type(str(base)) == "composer"
        or (base / "docroot").is_dir()
        or (base / "public").is_dir()
    )
    if composer_layout:
        return [
            base / "docroot" / "app" / "bundles",
            base / "public" / "app" / "bundles",
            base / "app" / "bundles",
        ]
    return [
        base / "app" / "bundles",
        base / "docroot" / "app" / "bundles",
        base / "public" / "app" / "bundles",
    ]
