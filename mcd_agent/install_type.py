from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


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


def detect_install_type(root: str) -> str:
    p = Path(root).resolve()
    parent = p.parent
    base = p.name.lower()

    # Custom composer-like layout used in production:
    # <instance>/config/local.php + <instance>/bin/console + web in <instance>/docroot.
    if (p / "config" / "local.php").exists() and (p / "bin" / "console").exists():
        if (p / "docroot").exists() or (p / "public").exists():
            return "composer"

    # If web root points directly to docroot/public, project root is parent.
    if base in {"docroot", "public"}:
        if (parent / "config" / "local.php").exists() and (parent / "bin" / "console").exists():
            return "composer"

    # Official composer pattern: recommended-project with web root in ./docroot.
    if _is_recommended_project(parent) and base in {"docroot", "public"}:
        return "composer"
    if _is_recommended_project(p):
        return "composer"

    # Conservative fallback:
    # package/zip installs contain composer files too, so do not classify as composer
    # by composer.lock alone to avoid false positives.
    return "zip"


def plugin_dir_candidates(root: str | Path) -> list[Path]:
    base = Path(root)
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
    """Return whether a plugin directory contains the minimum Mautic bundle shape.

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
