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


def campaign_sql_time_context(
    now_utc: datetime,
    mautic_timezone: str | None,
    mautic_major: int | None = None,
) -> dict[str, str]:
    """Build SQL placeholders without hard-coded timezone offsets.

    Modern Mautic Doctrine ``datetime`` mappings persist campaign timestamps in
    UTC. Mautic 4 installations in this fleet can persist scheduled event-log
    dates in instance-local time, so they retain the compatibility baseline.
    Unknown versions preserve that legacy behavior until discovery identifies
    the major version.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    now_local = mautic_local_datetime(now_utc, mautic_timezone)

    try:
        major = int(mautic_major or 0)
    except (TypeError, ValueError):
        major = 0
    legacy_local_event_log = major in {0, 4}
    campaign_now = now_local if legacy_local_event_log and now_local.utcoffset() != timedelta(0) else now_utc

    return {
        "now_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "now_local": now_local.strftime("%Y-%m-%d %H:%M:%S"),
        "now_event_log": campaign_now.strftime("%Y-%m-%d %H:%M:%S"),
        "window_start_utc_24h": (now_utc - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
        "window_start_utc_7d": (now_utc - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
        "window_start_local_24h": (now_local - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
        "window_start_local_7d": (now_local - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
    }
