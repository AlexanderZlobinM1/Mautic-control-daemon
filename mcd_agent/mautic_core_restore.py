from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from mcd_agent.models import MauticInstall


_PAGEHIT_PATCH_MARKER = "mcd pagehit cascade patch"
_PAGEHIT_REL_PATHS = [
    "app/bundles/PageBundle/Model/PageModel.php",
    "docroot/app/bundles/PageBundle/Model/PageModel.php",
    "public/app/bundles/PageBundle/Model/PageModel.php",
    "app/bundles/MessengerBundle/MessageHandler/PageHitNotificationHandler.php",
    "docroot/app/bundles/MessengerBundle/MessageHandler/PageHitNotificationHandler.php",
    "public/app/bundles/MessengerBundle/MessageHandler/PageHitNotificationHandler.php",
]

_M7_CAMPAIGN_TZ_MARKERS = [
    "DateTimeHelper::FORMAT_DB, 'local'",
    "$triggerDateViewTimezone = $this->getLocalTimezoneName();",
    "'model_timezone' => 'UTC',",
    "private function getLocalTimezoneName(): string",
    "private function normalizeTimeValue(",
]
_M7_CAMPAIGN_TZ_REL_PATHS = [
    "docroot/app/bundles/CampaignBundle/Controller/EventController.php",
    "app/bundles/CampaignBundle/Controller/EventController.php",
    "public/app/bundles/CampaignBundle/Controller/EventController.php",
    "docroot/app/bundles/CampaignBundle/Form/Type/EventType.php",
    "app/bundles/CampaignBundle/Form/Type/EventType.php",
    "public/app/bundles/CampaignBundle/Form/Type/EventType.php",
]


def _first_existing(root: str, rel_paths: list[str]) -> list[Path]:
    base = Path(root)
    out: list[Path] = []
    seen: set[Path] = set()
    for rel in rel_paths:
        p = base / rel
        if p in seen:
            continue
        seen.add(p)
        if p.exists() and p.is_file():
            out.append(p)
    return out


def _restore_from_backup(path: Path, backup_suffix: str, marker_predicate: Any) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"status": "error", "path": str(path), "reason": f"read_failed: {e}"}

    if not marker_predicate(text):
        return {"status": "clean", "path": str(path)}

    backup = path.with_name(path.name + backup_suffix)
    if not backup.exists():
        return {"status": "error", "path": str(path), "reason": f"backup_missing: {backup}"}

    try:
        st = path.stat()
        original = backup.read_text(encoding="utf-8", errors="ignore")
        path.write_text(original, encoding="utf-8")
        os.chmod(path, st.st_mode)
        try:
            os.chown(path, st.st_uid, st.st_gid)
        except PermissionError:
            pass
    except Exception as e:
        return {"status": "error", "path": str(path), "reason": f"restore_failed: {e}"}

    return {"status": "restored", "path": str(path), "backup": str(backup)}


def restore_retired_mcd_core_patches(install: MauticInstall) -> dict[str, Any]:
    """Restore Mautic core files modified by retired MCD core patches.

    This is intentionally narrow and only reverses patches that MCD itself used
    to apply. The Mautic 6 plugin metadata patch is not retired and is not
    touched here.
    """

    results: list[dict[str, Any]] = []

    for path in _first_existing(install.root, _PAGEHIT_REL_PATHS):
        results.append(
            _restore_from_backup(
                path,
                ".mcd-bak",
                lambda text: _PAGEHIT_PATCH_MARKER in text,
            )
        )

    for path in _first_existing(install.root, _M7_CAMPAIGN_TZ_REL_PATHS):
        results.append(
            _restore_from_backup(
                path,
                ".mcd-campaign-tz-bak",
                lambda text: any(marker in text for marker in _M7_CAMPAIGN_TZ_MARKERS),
            )
        )

    restored = [row for row in results if row.get("status") == "restored"]
    errors = [row for row in results if row.get("status") == "error"]
    if restored:
        logging.info("[%s] restored retired MCD core patches: %s", install.root, len(restored))
    return {
        "status": "error" if errors else ("restored" if restored else "clean"),
        "root": install.root,
        "restored": restored,
        "errors": errors,
    }
