from __future__ import annotations

import logging
import json
import os
from pathlib import Path
import re
from typing import Any

from mcd_agent.models import MauticInstall


_RELOAD_HELPER_REL_PATHS = [
    "app/bundles/PluginBundle/Helper/ReloadHelper.php",
    "docroot/app/bundles/PluginBundle/Helper/ReloadHelper.php",
    "public/app/bundles/PluginBundle/Helper/ReloadHelper.php",
]

_NULL_METADATA_RE = re.compile(
    r"(?P<prefix>\$metadata\s*=\s*\$pluginMetadata\[\$pluginConfig\['namespace'\]\]\s*\?\?\s*)null(?P<suffix>\s*;)"
)
_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_LOCAL_VERSION_RE = re.compile(r"['\"]version['\"]\s*=>\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_VERSION_CACHE: dict[str, tuple[float, str | None]] = {}


def _candidate_reload_helper(root: str) -> Path | None:
    base = Path(root)
    for rel in _RELOAD_HELPER_REL_PATHS:
        p = base / rel
        if p.exists() and p.is_file():
            return p
    return None


def _patch_backup_path(helper: Path) -> Path:
    return helper.with_name(helper.name + ".mcd-bak")


def _semver_tuple(raw: str | None) -> tuple[int, int, int] | None:
    if not raw:
        return None
    m = _SEMVER_RE.search(str(raw))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _detect_version_from_local_php(local_php_path: str | None) -> str | None:
    if not local_php_path:
        return None
    p = Path(local_php_path)
    if not p.exists() or not p.is_file():
        return None
    try:
        st = p.stat()
        key = str(p.resolve())
        cached = _VERSION_CACHE.get(key)
        if cached and cached[0] == float(st.st_mtime):
            return cached[1]
        text = p.read_text(encoding="utf-8", errors="ignore")
        m = _LOCAL_VERSION_RE.search(text)
        val = m.group(1).strip() if m else None
        _VERSION_CACHE[key] = (float(st.st_mtime), val)
        return val
    except Exception:
        return None


def _detect_version_from_composer_lock(root: str) -> str | None:
    lock = Path(root) / "composer.lock"
    if not lock.exists() or not lock.is_file():
        return None
    try:
        payload = json.loads(lock.read_text(encoding="utf-8"))
        packages = payload.get("packages")
        if not isinstance(packages, list):
            return None
        for pkg in packages:
            if not isinstance(pkg, dict):
                continue
            name = str(pkg.get("name", "")).strip().lower()
            if name not in {"mautic/core-lib", "mautic/core-bundle", "mautic/core"}:
                continue
            v = str(pkg.get("version", "")).strip()
            if _semver_tuple(v):
                return v
    except Exception:
        return None
    return None


def detect_mautic_version(install: MauticInstall) -> str | None:
    v = _detect_version_from_local_php(install.local_php_path)
    if v:
        return v
    return _detect_version_from_composer_lock(install.root)


def should_apply_m6_plugin_update_metadata_patch(
    install: MauticInstall,
    *,
    policy: str,
    version_min: str | None = None,
    version_max: str | None = None,
    apply_if_version_unknown: bool = True,
) -> dict[str, Any]:
    """
    Decide if Mautic 6 core metadata patch should be applied for this install.
    """
    if int(install.mautic_major or 0) != 6:
        return {"apply": False, "reason": "not_mautic_6", "version": None}
    pol = (policy or "required").strip().lower()
    if pol == "off":
        return {"apply": False, "reason": "policy_off", "version": None}
    if pol != "required":
        pol = "required"

    cur_version = detect_mautic_version(install)
    cur_v = _semver_tuple(cur_version)
    min_v = _semver_tuple(version_min)
    max_v = _semver_tuple(version_max)

    if cur_v is None:
        if apply_if_version_unknown:
            return {"apply": True, "reason": "version_unknown_apply", "version": cur_version}
        return {"apply": False, "reason": "version_unknown_skip", "version": cur_version}

    if min_v and cur_v < min_v:
        return {"apply": False, "reason": "below_min_version", "version": cur_version}
    if max_v and cur_v > max_v:
        return {"apply": False, "reason": "above_max_version", "version": cur_version}
    return {"apply": True, "reason": "in_version_scope", "version": cur_version}


def patch_status(install: MauticInstall) -> dict[str, Any]:
    if int(install.mautic_major or 0) != 6:
        return {"status": "skip", "reason": "not_mautic_6", "root": install.root}
    helper = _candidate_reload_helper(install.root)
    if not helper:
        return {"status": "skip", "reason": "reload_helper_not_found", "root": install.root}
    try:
        text = helper.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"status": "error", "reason": f"read_failed: {e}", "path": str(helper), "root": install.root}
    if "$pluginMetadata[$pluginConfig['namespace']] ?? [];" in text:
        return {
            "status": "patched",
            "path": str(helper),
            "backup": str(_patch_backup_path(helper)),
            "root": install.root,
        }
    if _NULL_METADATA_RE.search(text):
        return {
            "status": "vulnerable",
            "path": str(helper),
            "backup": str(_patch_backup_path(helper)),
            "root": install.root,
        }
    return {"status": "skip", "reason": "pattern_not_found", "path": str(helper), "root": install.root}


def ensure_m6_plugin_update_metadata_patch(install: MauticInstall) -> dict[str, Any]:
    """
    Fix Mautic 6 core bug in ReloadHelper:
    PluginUpdateEvent requires metadata array, but ReloadHelper may pass null.

    The patch is idempotent and safe for both zip and composer layouts.
    """
    if int(install.mautic_major or 0) != 6:
        return {"status": "skip", "reason": "not_mautic_6", "root": install.root}

    helper = _candidate_reload_helper(install.root)
    if not helper:
        return {"status": "skip", "reason": "reload_helper_not_found", "root": install.root}

    try:
        text = helper.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"status": "error", "reason": f"read_failed: {e}", "path": str(helper), "root": install.root}

    # Already patched.
    if "$pluginMetadata[$pluginConfig['namespace']] ?? [];" in text:
        return {"status": "already", "path": str(helper), "root": install.root}

    patched, n = _NULL_METADATA_RE.subn(r"\g<prefix>[]\g<suffix>", text, count=1)
    if n <= 0:
        return {"status": "skip", "reason": "pattern_not_found", "path": str(helper), "root": install.root}

    try:
        st = helper.stat()
        backup = helper.with_name(helper.name + ".mcd-bak")
        if not backup.exists():
            backup.write_text(text, encoding="utf-8")
            os.chmod(backup, st.st_mode)
            try:
                os.chown(backup, st.st_uid, st.st_gid)
            except PermissionError:
                pass

        helper.write_text(patched, encoding="utf-8")
        os.chmod(helper, st.st_mode)
        try:
            os.chown(helper, st.st_uid, st.st_gid)
        except PermissionError:
            pass
    except Exception as e:
        return {"status": "error", "reason": f"write_failed: {e}", "path": str(helper), "root": install.root}

    logging.info("[%s] mautic6 core patch applied: %s", install.root, helper)
    return {"status": "patched", "path": str(helper), "root": install.root}


def revert_m6_plugin_update_metadata_patch(install: MauticInstall) -> dict[str, Any]:
    if int(install.mautic_major or 0) != 6:
        return {"status": "skip", "reason": "not_mautic_6", "root": install.root}

    helper = _candidate_reload_helper(install.root)
    if not helper:
        return {"status": "skip", "reason": "reload_helper_not_found", "root": install.root}

    backup = _patch_backup_path(helper)
    if backup.exists():
        try:
            st = helper.stat()
            original = backup.read_text(encoding="utf-8", errors="ignore")
            helper.write_text(original, encoding="utf-8")
            os.chmod(helper, st.st_mode)
            try:
                os.chown(helper, st.st_uid, st.st_gid)
            except PermissionError:
                pass
            logging.info("[%s] mautic6 core patch reverted from backup: %s", install.root, helper)
            return {"status": "reverted", "path": str(helper), "root": install.root}
        except Exception as e:
            return {"status": "error", "reason": f"revert_failed: {e}", "path": str(helper), "root": install.root}

    try:
        text = helper.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"status": "error", "reason": f"read_failed: {e}", "path": str(helper), "root": install.root}

    reverted, n = re.subn(
        r"(?P<prefix>\$metadata\s*=\s*\$pluginMetadata\[\$pluginConfig\['namespace'\]\]\s*\?\?\s*)\[\](?P<suffix>\s*;)",
        r"\g<prefix>null\g<suffix>",
        text,
        count=1,
    )
    if n <= 0:
        return {"status": "skip", "reason": "already_unpatched_or_unknown", "path": str(helper), "root": install.root}

    try:
        st = helper.stat()
        helper.write_text(reverted, encoding="utf-8")
        os.chmod(helper, st.st_mode)
        try:
            os.chown(helper, st.st_uid, st.st_gid)
        except PermissionError:
            pass
    except Exception as e:
        return {"status": "error", "reason": f"write_failed: {e}", "path": str(helper), "root": install.root}

    logging.info("[%s] mautic6 core patch reverted by inline replace: %s", install.root, helper)
    return {"status": "reverted", "path": str(helper), "root": install.root}
