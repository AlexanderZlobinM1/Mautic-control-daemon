from __future__ import annotations

from collections import defaultdict
from typing import Any

from mcd_agent.db import MauticDB


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _table_columns(db: MauticDB, table: str) -> set[str]:
    with db._connect() as conn:
        with conn.cursor() as cur:
            return db._table_columns(cur, table)


def _fetch_campaign_email_events(db: MauticDB, campaign_id: int) -> dict[int, list[dict[str, Any]]]:
    prefix = str(db.cfg.table_prefix or "")
    table = db._safe_table(f"{prefix}campaign_events")
    query = (
        f"SELECT `id`, `name`, `type`, `channel`, `channel_id` "
        f"FROM `{table}` "
        "WHERE `campaign_id`=%s"
    )
    events_by_email: dict[int, list[dict[str, Any]]] = defaultdict(list)
    with db._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (int(campaign_id),))
            rows = cur.fetchall() or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        event_type = str(row.get("type") or "").strip().lower()
        channel = str(row.get("channel") or "").strip().lower()
        if event_type != "email.send" and channel != "email":
            continue
        email_id = _to_int(row.get("channel_id"))
        event_id = _to_int(row.get("id"))
        if email_id <= 0 or event_id <= 0:
            continue
        events_by_email[email_id].append(
            {
                "event_id": event_id,
                "name": str(row.get("name") or ""),
                "type": event_type,
                "channel": channel,
            }
        )
    return dict(events_by_email)


def _fetch_email_rows(db: MauticDB, email_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not email_ids:
        return {}
    prefix = str(db.cfg.table_prefix or "")
    table = db._safe_table(f"{prefix}emails")
    placeholders = ",".join(["%s"] * len(email_ids))
    query = (
        f"SELECT `id`, `name`, `sent_count`, `read_count` "
        f"FROM `{table}` "
        f"WHERE `id` IN ({placeholders})"
    )
    with db._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, tuple(email_ids))
            rows = cur.fetchall() or []
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict):
            eid = _to_int(row.get("id"))
            if eid > 0:
                out[eid] = row
    return out


def _fetch_stat_rows(db: MauticDB, email_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not email_ids:
        return {}
    prefix = str(db.cfg.table_prefix or "")
    table = db._safe_table(f"{prefix}email_stats")
    columns = _table_columns(db, table)
    select_parts = ["`email_id`", "COUNT(*) AS `actual_sent_count`"]
    if "lead_id" in columns:
        select_parts.append("COUNT(DISTINCT `lead_id`) AS `distinct_leads`")
    if "date_sent" in columns:
        select_parts.append("MIN(`date_sent`) AS `first_sent_at`")
        select_parts.append("MAX(`date_sent`) AS `last_sent_at`")
    if "is_failed" in columns:
        select_parts.append("SUM(CASE WHEN `is_failed` = 1 THEN 1 ELSE 0 END) AS `failed_count`")
    placeholders = ",".join(["%s"] * len(email_ids))
    query = (
        f"SELECT {', '.join(select_parts)} "
        f"FROM `{table}` "
        f"WHERE `email_id` IN ({placeholders}) "
        "GROUP BY `email_id`"
    )
    with db._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, tuple(email_ids))
            rows = cur.fetchall() or []
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict):
            eid = _to_int(row.get("email_id"))
            if eid > 0:
                out[eid] = row
    return out


def _fetch_campaign_progress_by_email(
    db: MauticDB,
    *,
    campaign_id: int,
    events_by_email: dict[int, list[dict[str, Any]]],
) -> dict[int, dict[str, Any]]:
    event_to_email: dict[int, int] = {}
    for email_id, events in events_by_email.items():
        for event in events:
            event_id = _to_int(event.get("event_id"))
            if event_id > 0:
                event_to_email[event_id] = int(email_id)
    if not event_to_email:
        return {}
    prefix = str(db.cfg.table_prefix or "")
    table = db._safe_table(f"{prefix}campaign_lead_event_log")
    event_ids = sorted(event_to_email)
    placeholders = ",".join(["%s"] * len(event_ids))
    query = (
        f"SELECT `event_id`, "
        "COUNT(*) AS `total_event_logs`, "
        "SUM(CASE WHEN `date_triggered` IS NULL THEN 1 ELSE 0 END) AS `pending_event_logs`, "
        "SUM(CASE WHEN `date_triggered` IS NOT NULL THEN 1 ELSE 0 END) AS `triggered_event_logs`, "
        "MAX(`date_triggered`) AS `max_triggered_at` "
        f"FROM `{table}` "
        f"WHERE `campaign_id`=%s AND `event_id` IN ({placeholders}) "
        "GROUP BY `event_id`"
    )
    totals: dict[int, dict[str, Any]] = defaultdict(
        lambda: {
            "total_event_logs": 0,
            "pending_event_logs": 0,
            "triggered_event_logs": 0,
            "max_triggered_at": None,
        }
    )
    with db._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (int(campaign_id), *event_ids))
            rows = cur.fetchall() or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        email_id = event_to_email.get(_to_int(row.get("event_id")))
        if email_id is None:
            continue
        total = totals[email_id]
        total["total_event_logs"] = _to_int(total.get("total_event_logs")) + _to_int(row.get("total_event_logs"))
        total["pending_event_logs"] = _to_int(total.get("pending_event_logs")) + _to_int(row.get("pending_event_logs"))
        total["triggered_event_logs"] = _to_int(total.get("triggered_event_logs")) + _to_int(row.get("triggered_event_logs"))
        max_triggered_at = row.get("max_triggered_at")
        if max_triggered_at and (
            total.get("max_triggered_at") is None or str(max_triggered_at) > str(total.get("max_triggered_at"))
        ):
            total["max_triggered_at"] = max_triggered_at
    return dict(totals)


def _fetch_global_pending_by_email(db: MauticDB, email_ids: list[int]) -> dict[int, int]:
    if not email_ids:
        return {}
    prefix = str(db.cfg.table_prefix or "")
    events_table = db._safe_table(f"{prefix}campaign_events")
    log_table = db._safe_table(f"{prefix}campaign_lead_event_log")
    placeholders = ",".join(["%s"] * len(email_ids))
    query = (
        "SELECT CAST(ce.`channel_id` AS UNSIGNED) AS `email_id`, COUNT(*) AS `pending_event_logs` "
        f"FROM `{log_table}` clel "
        f"INNER JOIN `{events_table}` ce ON ce.`id` = clel.`event_id` "
        "WHERE clel.`date_triggered` IS NULL "
        "AND (ce.`channel` = 'email' OR ce.`type` = 'email.send') "
        f"AND CAST(ce.`channel_id` AS UNSIGNED) IN ({placeholders}) "
        "GROUP BY CAST(ce.`channel_id` AS UNSIGNED)"
    )
    with db._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, tuple(email_ids))
            rows = cur.fetchall() or []
    out: dict[int, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        email_id = _to_int(row.get("email_id"))
        if email_id > 0:
            out[email_id] = _to_int(row.get("pending_event_logs"))
    return out


def _serializable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def audit_campaign_email_counters(db: MauticDB, campaign_id: int) -> dict[str, Any]:
    cid = int(campaign_id)
    events_by_email = _fetch_campaign_email_events(db, cid)
    email_ids = sorted(events_by_email)
    emails = _fetch_email_rows(db, email_ids)
    stats = _fetch_stat_rows(db, email_ids)
    progress = _fetch_campaign_progress_by_email(db, campaign_id=cid, events_by_email=events_by_email)
    global_pending = _fetch_global_pending_by_email(db, email_ids)

    rows: list[dict[str, Any]] = []
    for email_id in email_ids:
        email_row = emails.get(email_id, {})
        stat_row = stats.get(email_id, {})
        progress_row = progress.get(email_id, {})
        cached = _to_int(email_row.get("sent_count"))
        actual = _to_int(stat_row.get("actual_sent_count"))
        campaign_pending = _to_int(progress_row.get("pending_event_logs"))
        all_pending = _to_int(global_pending.get(email_id))
        drift = actual - cached
        status = "ok"
        repairable = False
        reason = ""
        if not email_row:
            status = "missing_email"
            reason = "email row missing"
        elif campaign_pending > 0:
            status = "pending"
            reason = "campaign has pending event-log work"
        elif all_pending > 0:
            status = "pending"
            reason = "email has pending event-log work"
        elif drift > 0:
            status = "drift"
            repairable = True
            reason = "cached sent_count is below email_stats"
        elif drift < 0:
            status = "drift"
            reason = "cached sent_count is above email_stats; skipped by safe upward-only policy"

        rows.append(
            {
                "email_id": email_id,
                "email_name": str(email_row.get("name") or ""),
                "event_ids": [int(event["event_id"]) for event in events_by_email.get(email_id, [])],
                "cached_sent_count": cached,
                "actual_sent_count": actual,
                "drift": drift,
                "read_count": _to_int(email_row.get("read_count")),
                "distinct_leads": _to_int(stat_row.get("distinct_leads")),
                "failed_count": _to_int(stat_row.get("failed_count")),
                "first_sent_at": _serializable(stat_row.get("first_sent_at")),
                "last_sent_at": _serializable(stat_row.get("last_sent_at")),
                "campaign_total_event_logs": _to_int(progress_row.get("total_event_logs")),
                "campaign_pending_event_logs": campaign_pending,
                "campaign_triggered_event_logs": _to_int(progress_row.get("triggered_event_logs")),
                "campaign_max_triggered_at": _serializable(progress_row.get("max_triggered_at")),
                "global_pending_event_logs": all_pending,
                "status": status,
                "repairable": repairable,
                "reason": reason,
            }
        )

    mismatches = sum(1 for row in rows if _to_int(row.get("drift")) != 0)
    repairable_count = sum(1 for row in rows if bool(row.get("repairable")))
    return {
        "status": "ok",
        "mode": "audit",
        "campaign_id": cid,
        "checked": len(rows),
        "mismatches": mismatches,
        "repairable": repairable_count,
        "emails": rows,
    }


def repair_campaign_email_counters(db: MauticDB, campaign_id: int) -> dict[str, Any]:
    payload = audit_campaign_email_counters(db, campaign_id)
    prefix = str(db.cfg.table_prefix or "")
    table = db._safe_table(f"{prefix}emails")
    repaired = 0
    skipped = 0
    with db._connect() as conn:
        with conn.cursor() as cur:
            for row in payload.get("emails", []):
                if not isinstance(row, dict) or not bool(row.get("repairable")):
                    if isinstance(row, dict) and _to_int(row.get("drift")) != 0:
                        skipped += 1
                    continue
                email_id = _to_int(row.get("email_id"))
                cached = _to_int(row.get("cached_sent_count"))
                actual = _to_int(row.get("actual_sent_count"))
                if email_id <= 0 or actual <= cached:
                    skipped += 1
                    continue
                affected = int(
                    cur.execute(
                        f"UPDATE `{table}` "
                        "SET `sent_count`=%s "
                        "WHERE `id`=%s AND `sent_count`=%s",
                        (actual, email_id, cached),
                    )
                    or 0
                )
                if affected == 1:
                    repaired += 1
                    row["repaired"] = True
                    row["new_sent_count"] = actual
                else:
                    skipped += 1
                    row["repaired"] = False
                    row["reason"] = "compare-and-set missed; counter changed during repair"
    payload["mode"] = "repair"
    payload["repaired"] = repaired
    payload["skipped"] = skipped
    return payload
