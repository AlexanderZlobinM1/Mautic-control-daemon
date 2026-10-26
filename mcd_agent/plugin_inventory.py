"""Canonical plugin-inventory observations and durable sync envelopes."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping


PLUGIN_INVENTORY_SCHEMA = "mcd-plugin-inventory-observation-v1"
PLUGIN_INVENTORY_SYNC_SCHEMA = "mcd-plugin-inventory-sync-v1"
PLUGIN_INVENTORY_RUNTIME_PREFIX = "plugin_inventory:"
PLUGIN_INVENTORY_PENDING_KEY = "plugin_inventory_sync_pending"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_inventory(rows: object) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if isinstance(rows, list):
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            bundle = str(raw.get("bundle") or "").strip()
            if not bundle:
                continue
            out.append({"bundle": bundle, "version": str(raw.get("version") or "-").strip() or "-"})
    return sorted(out, key=lambda item: (item["bundle"], item["version"]))


def inventory_sha256(rows: object) -> str:
    encoded = json.dumps(canonical_inventory(rows), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def runtime_key(instance_uid: str) -> str:
    return PLUGIN_INVENTORY_RUNTIME_PREFIX + str(instance_uid or "").strip()


def observation(
    *,
    instance_uid: str,
    root: str,
    inventory: object,
    observed_at_utc: str | None = None,
    status: str = "ok",
    error_type: str = "",
    error: str = "",
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_status = "ok" if str(status or "").strip().lower() == "ok" else "error"
    current = canonical_inventory(inventory)
    previous_inventory = canonical_inventory((previous or {}).get("inventory"))
    last_known = normalized_status != "ok"
    selected = previous_inventory if last_known else current
    previous_observed = str((previous or {}).get("observed_at_utc") or "").strip()
    payload: dict[str, Any] = {
        "schema": PLUGIN_INVENTORY_SCHEMA,
        "instance_uid": str(instance_uid or "").strip(),
        "root": str(root or "").strip(),
        "status": normalized_status,
        "complete": not last_known,
        "last_known": last_known,
        "inventory": selected,
        "inventory_sha256": inventory_sha256(selected),
        "observed_at_utc": str(observed_at_utc or utc_now()),
        "last_known_observed_at_utc": previous_observed if last_known else str(observed_at_utc or utc_now()),
        "scan_error": None,
    }
    if last_known:
        payload["scan_error"] = {
            "error_type": str(error_type or "plugin_inventory_scan_failed")[:120],
            "message": str(error or "plugin inventory scan failed")[:500],
        }
    return payload


def sync_payload(pending: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = pending if isinstance(pending, Mapping) else {}
    items = raw.get("items") if isinstance(raw.get("items"), Mapping) else {}
    normalized_items = []
    for uid, item in sorted(items.items(), key=lambda pair: str(pair[0])):
        if not isinstance(item, Mapping):
            continue
        normalized_items.append(
            {
                "instance_uid": str(uid),
                "root": str(item.get("root") or ""),
                "inventory_sha256": str(item.get("inventory_sha256") or ""),
                "observed_at_utc": str(item.get("observed_at_utc") or ""),
            }
        )
    status = "pending" if normalized_items else "ok"
    return {
        "schema": PLUGIN_INVENTORY_SYNC_SCHEMA,
        "status": status,
        "items": normalized_items,
        "requested_at_utc": str(raw.get("requested_at_utc") or ""),
        "last_attempt_at_utc": str(raw.get("last_attempt_at_utc") or ""),
        "retry_count": int(raw.get("retry_count", 0) or 0),
        "last_error": str(raw.get("last_error") or "")[:500],
        "operation_id": str(raw.get("operation_id") or ""),
    }
