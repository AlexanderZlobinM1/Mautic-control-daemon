from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcd_agent.models import MauticInstall


_PLUGIN_REL_PATHS = (
    "plugins/GrapesJsBuilderBundle",
    "docroot/plugins/GrapesJsBuilderBundle",
    "public/plugins/GrapesJsBuilderBundle",
)
_SOURCE_REL_PATH = "Assets/library/js/plugins/grapesjs-ckeditor/editorLifecycle.js"
_BUILDER_SERVICE_REL_PATH = "Assets/library/js/builder.service.js"
_DIST_REL_PATH = "Assets/library/js/dist/builder.js"
_ASSETS_SUBSCRIBER_REL_PATH = "EventSubscriber/AssetsSubscriber.php"
_SOURCE_VULNERABLE = "licenseKey: this.licenseKey,"
_SOURCE_PATCHED = "licenseKey: this.licenseKey || 'GPL',"
_DIST_VULNERABLE = "licenseKey:this.licenseKey"
_DIST_PATCHED = 'licenseKey:this.licenseKey||"GPL"'
_EMAIL_HTML_SOURCE_VULNERABLE = """grapesjsmautic: BuilderService.getMauticConf('email-html'),
        [grapesjsckeditor]: {
          ckeditor_module: ckeditorModuleUrl,
          inlineMode: true,"""
_EMAIL_HTML_SOURCE_PATCHED = """grapesjsmautic: BuilderService.getMauticConf('email-html'),
        [grapesjsckeditor]: {
          ckeditor_module: ckeditorModuleUrl,
          licenseKey: 'GPL',
          inlineMode: true,"""
_EMAIL_HTML_DIST_VULNERABLE = 'grapesjsmautic:gl.getMauticConf("email-html"),[mT]:{ckeditor_module:r,inlineMode:!0'
_EMAIL_HTML_DIST_PATCHED = 'grapesjsmautic:gl.getMauticConf("email-html"),[mT]:{ckeditor_module:r,licenseKey:"GPL",inlineMode:!0'
_ASSET_URL_VULNERABLE = "$assetsEvent->addScript('plugins/GrapesJsBuilderBundle/Assets/library/js/dist/builder.js');"
_ASSET_URL_PATCHED = "$assetsEvent->addScript('plugins/GrapesJsBuilderBundle/Assets/library/js/dist/builder.js?mcd-ckeditor-gpl');"


def _plugin_root(root: str) -> Path | None:
    base = Path(root)
    for rel in _PLUGIN_REL_PATHS:
        candidate = base / rel
        if candidate.is_dir():
            return candidate
    return None


def _patch_file(path: Path, vulnerable: str, patched: str) -> dict[str, Any]:
    try:
        original = path.read_text(encoding="utf-8", errors="strict")
    except FileNotFoundError:
        return {"status": "skip", "reason": "file_not_found", "path": str(path)}
    except Exception as exc:
        return {"status": "error", "reason": f"read_failed: {exc}", "path": str(path)}

    patched_count = original.count(patched)
    vulnerable_count = original.replace(patched, "").count(vulnerable)
    if patched_count == 1 and vulnerable_count == 0:
        return {"status": "already", "path": str(path)}
    if patched_count == 0 and vulnerable_count == 0:
        return {"status": "skip", "reason": "pattern_not_found", "path": str(path)}
    if patched_count or vulnerable_count != 1:
        return {
            "status": "error",
            "reason": f"unexpected_signature_counts: vulnerable={vulnerable_count} patched={patched_count}",
            "path": str(path),
        }

    replacement = original.replace(vulnerable, patched, 1)
    try:
        stat = path.stat()
        backup = path.with_name(path.name + ".mcd-bak")
        if not backup.exists():
            backup.write_text(original, encoding="utf-8")
            os.chmod(backup, stat.st_mode)
            try:
                os.chown(backup, stat.st_uid, stat.st_gid)
            except PermissionError:
                pass
        path.write_text(replacement, encoding="utf-8")
        os.chmod(path, stat.st_mode)
        try:
            os.chown(path, stat.st_uid, stat.st_gid)
        except PermissionError:
            pass
    except Exception as exc:
        return {"status": "error", "reason": f"write_failed: {exc}", "path": str(path)}

    return {"status": "patched", "path": str(path), "backup": str(backup)}


def ensure_grapesjs_ckeditor_gpl_patch(install: MauticInstall) -> dict[str, Any]:
    """Restore GrapesJS rich-text editing when Mautic 7 omits a CKEditor key."""
    if int(install.mautic_major or 0) != 7:
        return {"status": "skip", "reason": "not_mautic_7", "root": install.root}

    plugin_root = _plugin_root(install.root)
    if plugin_root is None:
        return {"status": "skip", "reason": "plugin_not_found", "root": install.root}

    files = [
        _patch_file(plugin_root / _SOURCE_REL_PATH, _SOURCE_VULNERABLE, _SOURCE_PATCHED),
        _patch_file(
            plugin_root / _BUILDER_SERVICE_REL_PATH,
            _EMAIL_HTML_SOURCE_VULNERABLE,
            _EMAIL_HTML_SOURCE_PATCHED,
        ),
        _patch_file(plugin_root / _DIST_REL_PATH, _DIST_VULNERABLE, _DIST_PATCHED),
        _patch_file(plugin_root / _DIST_REL_PATH, _EMAIL_HTML_DIST_VULNERABLE, _EMAIL_HTML_DIST_PATCHED),
        _patch_file(plugin_root / _ASSETS_SUBSCRIBER_REL_PATH, _ASSET_URL_VULNERABLE, _ASSET_URL_PATCHED),
    ]
    if any(item["status"] == "error" for item in files):
        status = "error"
    elif any(item["status"] == "patched" for item in files):
        status = "patched"
    elif all(item["status"] == "already" for item in files):
        status = "already"
    else:
        status = "skip"
    return {"status": status, "root": install.root, "plugin_root": str(plugin_root), "files": files}
