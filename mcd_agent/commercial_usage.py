from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcd_agent.db import MauticDB


def _non_negative(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def collect_commercial_usage(db: MauticDB, *, observed_at: datetime | None = None) -> dict[str, Any]:
    """Read-only commercial metering seam.

    The values deliberately come from source rows, not denormalized counters:
    leads is the current contact inventory and email_stats.date_sent is the
    cumulative successful-send event source. Delivery, cadence, signatures,
    and billing remain outside this collector.
    """
    prefix = str(db.cfg.table_prefix or "")
    leads = db._safe_table(f"{prefix}leads")
    email_stats = db._safe_table(f"{prefix}email_stats")
    contacts = db.fetch_rows(f"SELECT COUNT(*) AS current_contacts FROM `{leads}`", limit=1)
    sends = db.fetch_rows(
        f"SELECT COUNT(*) AS successful_sends FROM `{email_stats}` "
        "WHERE date_sent IS NOT NULL AND COALESCE(is_failed, 0) = 0",
        limit=1,
    )
    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    return {
        "observed_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contacts_current": _non_negative((contacts[0] if contacts else {}).get("current_contacts")),
        "successful_sends_cumulative": _non_negative((sends[0] if sends else {}).get("successful_sends")),
        "sources": {
            "contacts_current": "leads",
            "successful_sends_cumulative": "email_stats.date_sent",
        },
    }
