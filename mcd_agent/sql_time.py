from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def mautic_local_datetime(now_utc: datetime, mautic_timezone: str | None) -> datetime:
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    tz_name = str(mautic_timezone or "").strip()
    if tz_name:
        try:
            return now_utc.astimezone(ZoneInfo(tz_name))
        except Exception:
            return now_utc
    return now_utc


def campaign_sql_time_context(now_utc: datetime, mautic_timezone: str | None) -> dict[str, str]:
    """Build SQL placeholders without hard-coded timezone offsets.

    Mautic campaign DATETIME columns are stored without timezone metadata and
    installations differ between UTC and instance-local storage depending on
    Mautic/DB/PHP runtime history. Expose both clocks so SQL can be tolerant
    without hard-coded hour offsets.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    now_local = mautic_local_datetime(now_utc, mautic_timezone)

    return {
        "now_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "now_local": now_local.strftime("%Y-%m-%d %H:%M:%S"),
        "window_start_utc_24h": (now_utc - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
        "window_start_utc_7d": (now_utc - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
        "window_start_local_24h": (now_local - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
        "window_start_local_7d": (now_local - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
    }
