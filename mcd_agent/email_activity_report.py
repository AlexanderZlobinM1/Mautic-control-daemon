from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from mcd_agent.db import MauticDB


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    return value


def _clean_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{str(k): _to_jsonable(v) for k, v in row.items()} for row in rows if isinstance(row, dict)]


def _contact_filter_sql(alias: str, mode: str) -> str:
    if mode == "fresh":
        return f"AND {alias}.date_added >= DATE_SUB(CURDATE(), INTERVAL %s DAY)"
    if mode == "old":
        return f"AND {alias}.date_added < DATE_SUB(CURDATE(), INTERVAL %s DAY)"
    return ""


def _contact_join_sql(alias: str, source_alias: str, mode: str) -> str:
    if mode not in {"fresh", "old"}:
        return ""
    return f"JOIN {{leads}} {alias} ON {alias}.id = {source_alias}.lead_id"


def _contact_label(mode: str, contact_age_days: int) -> str:
    if mode == "fresh":
        return f"contacts added in the last {contact_age_days} days"
    if mode == "old":
        return f"contacts added more than {contact_age_days} days ago"
    return "all contacts"


def _fetch(db: MauticDB, query: str, params: list[Any]) -> list[dict[str, Any]]:
    with db._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
    return _clean_rows([row for row in rows if isinstance(row, dict)])


def collect_email_activity_report(
    db: MauticDB,
    *,
    days: int = 7,
    include_summary: bool = True,
    include_extended: bool = True,
    contact_mode: str = "",
    contact_age_days: int = 30,
) -> dict[str, Any]:
    days = max(1, min(3660, int(days or 7)))
    window_days = max(0, days - 1)
    contact_age_days = max(1, min(3660, int(contact_age_days or 30)))
    mode = str(contact_mode or "").strip().lower()
    if mode in {"all", "any", "none"}:
        mode = ""
    if mode not in {"", "fresh", "old"}:
        raise ValueError("contact_mode must be one of all, fresh, old")

    prefix = str(db.cfg.table_prefix or "")
    tables = {
        "email_stats": db._safe_table(f"{prefix}email_stats"),
        "emails": db._safe_table(f"{prefix}emails"),
        "page_hits": db._safe_table(f"{prefix}page_hits"),
        "lead_donotcontact": db._safe_table(f"{prefix}lead_donotcontact"),
        "leads": db._safe_table(f"{prefix}leads"),
    }

    def render(sql: str) -> str:
        for key, table in tables.items():
            sql = sql.replace("{" + key + "}", f"`{table}`")
        return sql

    contact_join_es = _contact_join_sql("l", "es", mode)
    contact_join_ph = _contact_join_sql("lph", "ph", mode)
    contact_join_dc = _contact_join_sql("ldc", "dnc", mode)
    contact_where_es = _contact_filter_sql("l", mode)
    contact_where_ph = _contact_filter_sql("lph", mode)
    contact_where_dc = _contact_filter_sql("ldc", mode)

    summary_rows: list[dict[str, Any]] = []
    extended_rows: list[dict[str, Any]] = []
    if include_summary:
        params: list[Any] = [window_days]
        if mode:
            params.append(contact_age_days)
        params.append(window_days)
        if mode:
            params.append(contact_age_days)
        params.append(window_days)
        if mode:
            params.append(contact_age_days)
        summary_query = render(
            f"""
            SELECT DATE(es.date_sent) AS day,
                   COUNT(*) AS sent,
                   SUM(es.is_read=1) AS read_cnt,
                   SUM(es.is_failed=1) AS failed_cnt,
                   IFNULL(ph.clicks,0) AS clicks,
                   IFNULL(dc.unsubscribed,0) AS unsubscribed
            FROM {{email_stats}} es
            {contact_join_es}
            LEFT JOIN (
              SELECT DATE(ph.date_hit) AS day, COUNT(*) AS clicks
              FROM {{page_hits}} ph
              {contact_join_ph}
              WHERE ph.email_id IS NOT NULL
                AND ph.date_hit >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                {contact_where_ph}
              GROUP BY DATE(ph.date_hit)
            ) ph ON ph.day = DATE(es.date_sent)
            LEFT JOIN (
              SELECT DATE(dnc.date_added) AS day, SUM(dnc.reason=1) AS unsubscribed
              FROM {{lead_donotcontact}} dnc
              {contact_join_dc}
              WHERE dnc.channel='email'
                AND dnc.date_added >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                {contact_where_dc}
              GROUP BY DATE(dnc.date_added)
            ) dc ON dc.day = DATE(es.date_sent)
            WHERE es.date_sent >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            {contact_where_es}
            GROUP BY DATE(es.date_sent), ph.clicks, dc.unsubscribed
            ORDER BY day DESC
            """
        )
        summary_rows = _fetch(db, summary_query, params)

    if include_extended:
        params = [window_days]
        if mode:
            params.append(contact_age_days)
        params.append(window_days)
        if mode:
            params.append(contact_age_days)
        params.append(window_days)
        if mode:
            params.append(contact_age_days)
        extended_query = render(
            f"""
            SELECT DATE(es.date_sent) AS day,
                   es.email_id,
                   COALESCE(e.name,'(no name)') AS email_name,
                   COUNT(*) AS sent,
                   SUM(es.is_read=1) AS read_cnt,
                   SUM(es.is_failed=1) AS failed_cnt,
                   IFNULL(ph.clicks,0) AS clicks,
                   IFNULL(dc.unsubscribed,0) AS unsubscribed
            FROM {{email_stats}} es
            {contact_join_es}
            LEFT JOIN {{emails}} e ON e.id=es.email_id
            LEFT JOIN (
              SELECT DATE(ph.date_hit) AS day, ph.email_id, COUNT(*) AS clicks
              FROM {{page_hits}} ph
              {contact_join_ph}
              WHERE ph.email_id IS NOT NULL
                AND ph.date_hit >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                {contact_where_ph}
              GROUP BY DATE(ph.date_hit), ph.email_id
            ) ph ON ph.day = DATE(es.date_sent) AND ph.email_id = es.email_id
            LEFT JOIN (
              SELECT DATE(dnc.date_added) AS day,
                     CAST(dnc.channel_id AS UNSIGNED) AS email_id,
                     SUM(dnc.reason=1) AS unsubscribed
              FROM {{lead_donotcontact}} dnc
              {contact_join_dc}
              WHERE dnc.channel='email'
                AND dnc.channel_id IS NOT NULL
                AND dnc.date_added >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                {contact_where_dc}
              GROUP BY DATE(dnc.date_added), CAST(dnc.channel_id AS UNSIGNED)
            ) dc ON dc.day = DATE(es.date_sent) AND dc.email_id = es.email_id
            WHERE es.date_sent >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            {contact_where_es}
            GROUP BY DATE(es.date_sent), es.email_id, e.name, ph.clicks, dc.unsubscribed
            ORDER BY day DESC, sent DESC
            """
        )
        extended_rows = _fetch(db, extended_query, params)

    total_source = summary_rows if summary_rows else extended_rows
    summary_totals = {
        "sent": sum(int(row.get("sent") or 0) for row in total_source),
        "read": sum(int(row.get("read_cnt") or 0) for row in total_source),
        "failed": sum(int(row.get("failed_cnt") or 0) for row in total_source),
        "clicks": sum(int(row.get("clicks") or 0) for row in total_source),
        "unsubscribed": sum(int(row.get("unsubscribed") or 0) for row in total_source),
    }

    return {
        "status": "ok",
        "days": days,
        "contact_mode": mode or "all",
        "contact_age_days": contact_age_days,
        "contact_label": _contact_label(mode, contact_age_days),
        "include_summary": bool(include_summary),
        "include_extended": bool(include_extended),
        "summary_rows": summary_rows,
        "extended_rows": extended_rows,
        "summary_totals": summary_totals,
    }
