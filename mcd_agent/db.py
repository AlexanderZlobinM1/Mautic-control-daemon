from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pymysql

from mcd_agent.models import DBConfig


_PAGE_HIT_NOTIFICATION_CLASS = "Mautic\\MessengerBundle\\Message\\PageHitNotification"
_PAGE_HIT_NOTIFICATION_CLASS_ESCAPED = _PAGE_HIT_NOTIFICATION_CLASS.replace("\\", "\\\\")
_PAGE_HIT_ID_RE = re.compile(r's:\d+:"(?:\x00[^"]+\x00)?hitId";i:(\d+);')
_PAGE_HIT_ID_JSON_RE = re.compile(r'"hit_?id"\s*:\s*(\d+)', re.IGNORECASE)


class MauticDB:
    def __init__(self, cfg: DBConfig) -> None:
        self.cfg = cfg

    @staticmethod
    def _is_local_host(host: str | None) -> bool:
        h = str(host or "").strip().lower()
        return h in {"", "_", "localhost", "127.0.0.1", "::1"}

    @staticmethod
    def _socket_candidates() -> list[str]:
        candidates = [
            "/var/run/mysqld/mysqld.sock",
            "/run/mysqld/mysqld.sock",
            "/tmp/mysql.sock",
            "/var/lib/mysql/mysql.sock",
        ]
        out: list[str] = []
        for raw in candidates:
            p = Path(raw)
            if p.exists() and p.is_socket():
                out.append(raw)
        return out

    def _connect_variants(self) -> list[dict[str, Any]]:
        base: dict[str, Any] = {
            "user": self.cfg.user,
            "password": self.cfg.password,
            "database": self.cfg.name,
            "charset": "utf8mb4",
            "autocommit": True,
            "cursorclass": pymysql.cursors.DictCursor,
            "connect_timeout": 5,
            # Some SQL-driven segment rebuild rules can run for minutes on very
            # large page_hits/lead tables. Keep DB socket alive long enough to
            # avoid false "Lost connection ... timed out" failures mid-query.
            "read_timeout": 1800,
            "write_timeout": 1800,
        }
        host_raw = str(self.cfg.host or "").strip()
        port = int(self.cfg.port or 3306)
        out: list[dict[str, Any]] = []
        seen: set[tuple[tuple[str, str], ...]] = set()

        def _add(kwargs: dict[str, Any]) -> None:
            key = tuple(sorted((str(k), str(v)) for k, v in kwargs.items()))
            if key in seen:
                return
            seen.add(key)
            out.append(kwargs)

        _add({**base, "host": host_raw or "localhost", "port": port})
        if self._is_local_host(host_raw):
            # Prefer unix socket on local DBs because many hosts have grants
            # only for user@localhost and reject user@127.0.0.1.
            for sock in self._socket_candidates():
                _add({**base, "unix_socket": sock})
            _add({**base, "host": "localhost", "port": port})
            _add({**base, "host": "127.0.0.1", "port": port})
        return out

    def _connect(self) -> pymysql.connections.Connection:
        # Bound DB operations so one stuck query/socket cannot freeze loops.
        # Local DB auth layouts differ (localhost vs 127.0.0.1 grants), so we
        # try safe local fallbacks (including unix socket) before failing.
        last_error: Exception | None = None
        for kwargs in self._connect_variants():
            try:
                return pymysql.connect(**kwargs)
            except Exception as e:
                last_error = e
                continue
        if last_error is not None:
            raise last_error
        raise RuntimeError("mysql_connection_failed")

    @staticmethod
    def _safe_column(name: str) -> str:
        raw = str(name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]+", raw):
            raise ValueError(f"unsafe column name: {name!r}")
        return raw

    def _table_columns(self, cur: Any, table: str) -> set[str]:
        cur.execute(f"SHOW COLUMNS FROM `{table}`")
        rows = cur.fetchall() or []
        out: set[str] = set()
        for row in rows:
            if isinstance(row, dict):
                name = row.get("Field")
            else:
                name = row[0] if row else None
            if name:
                out.add(str(name))
        return out

    def _render_query(self, query_template: str, context: dict[str, str] | None = None) -> str:
        now_utc = datetime.now(timezone.utc)
        params: dict[str, str] = {
            "prefix": self.cfg.table_prefix,
            "now_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "window_start_utc_24h": (now_utc - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
            "window_start_utc_7d": (now_utc - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        if context:
            params.update(context)
        return query_template.format(**params)

    def fetch_ids(self, query_template: str, limit: int, context: dict[str, str] | None = None) -> list[int]:
        query = self._render_query(query_template, context=context)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
        out: list[int] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            first = next(iter(row.values()), None)
            try:
                out.append(int(first))
            except (TypeError, ValueError):
                continue
            if len(out) >= limit:
                break
        return out

    def fetch_rows(
        self,
        query_template: str,
        limit: int = 5000,
        context: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        query = self._render_query(query_template, context=context)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                out.append(row)
            if len(out) >= limit:
                break
        return out

    def fetch_count(self, query_template: str, context: dict[str, str] | None = None) -> int:
        query = self._render_query(query_template, context=context)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                row: Any = cur.fetchone()
        if not isinstance(row, dict):
            return 0
        value = next(iter(row.values()), 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def fetch_segment_definitions(self, segment_ids: list[int]) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(int(x) for x in segment_ids if int(x) > 0))
        if not ids:
            return []
        prefix = str(self.cfg.table_prefix or "")
        table_segments = self._safe_table(f"{prefix}lead_lists")
        placeholders = ",".join(["%s"] * len(ids))
        query = (
            f"SELECT id, name, filters, checked_out, checked_out_by_user, "
            f"date_modified, last_built_date "
            f"FROM `{table_segments}` "
            f"WHERE id IN ({placeholders})"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(ids))
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                out.append(row)
        return out

    def fetch_published_segment_filters(self) -> list[dict[str, Any]]:
        prefix = str(self.cfg.table_prefix or "")
        table_segments = self._safe_table(f"{prefix}lead_lists")
        query = (
            f"SELECT id, name, filters, date_modified, last_built_date "
            f"FROM `{table_segments}` "
            f"WHERE is_published = 1 "
            f"ORDER BY id ASC"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                out.append(row)
        return out

    def fetch_lead_columns(self) -> set[str]:
        prefix = str(self.cfg.table_prefix or "")
        table_leads = f"{prefix}leads"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COLUMN_NAME
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = %s
                    """,
                    (table_leads,),
                )
                rows = cur.fetchall()
        out: set[str] = set()
        for row in rows:
            if isinstance(row, dict):
                raw = str(row.get("COLUMN_NAME") or row.get("column_name") or "").strip()
                if raw:
                    out.add(raw)
        return out

    def table_has_column(self, table_template: str, column: str) -> bool:
        table = str(table_template or "").format(prefix=str(self.cfg.table_prefix or ""))
        column = self._safe_column(column)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = %s
                      AND COLUMN_NAME = %s
                    LIMIT 1
                    """,
                    (table, column),
                )
                row = cur.fetchone()
        return bool(row)

    def execute_sql_template(self, query_template: str, context: dict[str, str] | None = None) -> int:
        query = self._render_query(query_template, context=context)
        with self._connect() as conn:
            with conn.cursor() as cur:
                affected = cur.execute(query)
        try:
            return int(affected)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _apply_statement_timeout(cur: Any, timeout_sec: int | None) -> None:
        """Best-effort per-session cap for SQL segment rebuild statements."""
        try:
            seconds = int(timeout_sec or 0)
        except (TypeError, ValueError):
            seconds = 0
        if seconds <= 0:
            return

        # MariaDB supports max_statement_time in seconds; MySQL supports
        # max_execution_time in milliseconds. Ignore unsupported variables so
        # mixed fleets keep working.
        for query, value in (
            ("SET SESSION max_statement_time=%s", seconds),
            ("SET SESSION max_execution_time=%s", seconds * 1000),
        ):
            try:
                cur.execute(query, (value,))
            except Exception:
                continue

    def rebuild_segment_membership(
        self,
        *,
        segment_id: int,
        select_query_template: str,
        context: dict[str, str] | None = None,
        statement_timeout_sec: int | None = 1800,
    ) -> dict[str, Any]:
        """
        Rebuild one segment directly in DB:
        - execute provided SELECT (must expose `lead_id` column)
        - replace rows in <prefix>lead_lists_leads for this segment
        - update <prefix>lead_lists build metadata
        """
        select_query = self._render_query(select_query_template, context=context).strip()
        if select_query.endswith(";"):
            select_query = select_query[:-1].strip()
        if not select_query:
            raise ValueError("empty_select_query")

        prefix = str(self.cfg.table_prefix or "")
        table_segment_links = self._safe_table(f"{prefix}lead_lists_leads")
        table_segments = self._safe_table(f"{prefix}lead_lists")
        temp_table = "mcd_tmp_segment_leads"
        sid = int(segment_id)
        started = time.monotonic()

        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    self._apply_statement_timeout(cur, statement_timeout_sec)
                    segment_columns = self._table_columns(cur, table_segments)
                    cur.execute(
                        f"CREATE TEMPORARY TABLE IF NOT EXISTS `{temp_table}` ("
                        " `lead_id` BIGINT UNSIGNED NOT NULL,"
                        " PRIMARY KEY (`lead_id`)"
                        ") ENGINE=InnoDB"
                    )
                    cur.execute(f"TRUNCATE TABLE `{temp_table}`")
                    cur.execute(
                        "INSERT IGNORE INTO `{tmp}` (`lead_id`) "
                        "SELECT DISTINCT CAST(src.lead_id AS UNSIGNED) "
                        "FROM ({sel}) src "
                        "WHERE src.lead_id IS NOT NULL".format(tmp=temp_table, sel=select_query)
                    )
                    cur.execute(f"SELECT COUNT(*) AS cnt FROM `{temp_table}`")
                    row = cur.fetchone()
                    selected_count = int((row or {}).get("cnt") or 0)

                    conn.autocommit(False)
                    cur.execute(f"DELETE FROM `{table_segment_links}` WHERE `leadlist_id`=%s", (sid,))
                    deleted_count = int(cur.rowcount or 0)
                    cur.execute(
                        "INSERT INTO `{tbl}` "
                        "(`leadlist_id`,`lead_id`,`date_added`,`manually_removed`,`manually_added`) "
                        "SELECT %s, t.lead_id, NOW(), 0, 0 FROM `{tmp}` t".format(
                            tbl=table_segment_links,
                            tmp=temp_table,
                        ),
                        (sid,),
                    )
                    inserted_count = int(cur.rowcount or 0)
                    elapsed_sec = max(0.01, float(time.monotonic() - started))

                    # Mautic treats a segment as needing rebuild when
                    # date_modified >= last_built_date. Keep the real
                    # definition-modified timestamp if present, but guarantee
                    # the built marker is strictly newer than it.
                    set_clauses: list[str] = []
                    params: list[Any] = []
                    for col in ("checked_out", "checked_out_by", "checked_out_by_user"):
                        if col in segment_columns:
                            set_clauses.append(f"`{self._safe_column(col)}`=NULL")
                    if "date_modified" in segment_columns:
                        set_clauses.append(
                            "`date_modified`=CASE "
                            "WHEN `date_modified` IS NULL OR `date_modified` >= NOW() "
                            "THEN DATE_SUB(NOW(), INTERVAL 1 SECOND) "
                            "ELSE `date_modified` END"
                        )
                    if "last_built_date" in segment_columns:
                        set_clauses.append("`last_built_date`=NOW()")
                    if "last_built_time" in segment_columns:
                        set_clauses.append("`last_built_time`=%s")
                        params.append(elapsed_sec)
                    if set_clauses:
                        params.append(sid)
                        cur.execute(
                            f"UPDATE `{table_segments}` SET {', '.join(set_clauses)} WHERE `id`=%s",
                            tuple(params),
                        )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    conn.autocommit(True)
                except Exception:
                    pass
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"DROP TEMPORARY TABLE IF EXISTS `{temp_table}`")
                except Exception:
                    pass

        return {
            "segment_id": sid,
            "selected_count": selected_count,
            "deleted_count": deleted_count,
            "inserted_count": inserted_count,
            "duration_sec": max(0.0, float(time.monotonic() - started)),
        }

    def cleanup_stale_checked_out_locks(
        self,
        *,
        cutoff_utc: str,
        max_rows: int = 20,
        skip_segment_ids: set[int] | None = None,
        skip_campaign_ids: set[int] | None = None,
    ) -> dict[str, Any]:
        prefix = str(self.cfg.table_prefix or "")
        table_segments = self._safe_table(f"{prefix}lead_lists")
        table_campaigns = self._safe_table(f"{prefix}campaigns")
        seg_limit = max(1, int(max_rows))
        camp_limit = max(1, int(max_rows))
        skip_segment_ids = {int(x) for x in (skip_segment_ids or set()) if int(x) > 0}
        skip_campaign_ids = {int(x) for x in (skip_campaign_ids or set()) if int(x) > 0}

        def _fetch_rows(table: str, limit: int) -> list[dict[str, Any]]:
            query = (
                f"SELECT id, name, checked_out, checked_out_by_user "
                f"FROM `{table}` "
                f"WHERE checked_out IS NOT NULL AND checked_out < %s "
                f"ORDER BY checked_out ASC, id ASC "
                f"LIMIT {limit}"
            )
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (str(cutoff_utc),))
                    rows = cur.fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                if isinstance(row, dict):
                    out.append(row)
            return out

        def _clear_rows(table: str, ids: list[int]) -> int:
            if not ids:
                return 0
            placeholders = ",".join(["%s"] * len(ids))
            query = (
                f"UPDATE `{table}` "
                "SET `checked_out`=NULL, `checked_out_by`=NULL, `checked_out_by_user`=NULL "
                f"WHERE id IN ({placeholders})"
            )
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, tuple(int(x) for x in ids))
                    affected = int(cur.rowcount or 0)
            return affected

        stale_segments = [row for row in _fetch_rows(table_segments, seg_limit) if int(row.get("id") or 0) not in skip_segment_ids]
        stale_campaigns = [row for row in _fetch_rows(table_campaigns, camp_limit) if int(row.get("id") or 0) not in skip_campaign_ids]

        cleared_segments = _clear_rows(table_segments, [int(row.get("id") or 0) for row in stale_segments])
        cleared_campaigns = _clear_rows(table_campaigns, [int(row.get("id") or 0) for row in stale_campaigns])

        return {
            "segments": stale_segments,
            "campaigns": stale_campaigns,
            "cleared_segments": int(cleared_segments),
            "cleared_campaigns": int(cleared_campaigns),
            "cutoff_utc": str(cutoff_utc),
        }

    @staticmethod
    def _safe_ident(raw: str) -> str:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", raw or ""):
            raise ValueError(f"Unsafe SQL identifier: {raw!r}")
        return raw

    @staticmethod
    def _safe_table(raw: str) -> str:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", raw or ""):
            raise ValueError(f"Unsafe SQL table: {raw!r}")
        return raw

    def count_contacts_without_comm(
        self,
        *,
        email_field: str,
        phone_field: str,
        mode: str = "email_and_mobile",
    ) -> int:
        table = self._safe_table(f"{self.cfg.table_prefix}leads")
        email = self._safe_ident(email_field)
        if str(mode).strip().lower() == "email_only":
            where = f"(`{email}` IS NULL OR `{email}` = '')"
        else:
            phone = self._safe_ident(phone_field)
            where = (
                f"(`{email}` IS NULL OR `{email}` = '') "
                f"AND (`{phone}` IS NULL OR `{phone}` = '')"
            )
        query = f"SELECT COUNT(*) AS cnt FROM `{table}` WHERE {where}"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                row: Any = cur.fetchone()
        if not isinstance(row, dict):
            return 0
        value = next(iter(row.values()), 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def delete_contacts_without_comm(
        self,
        *,
        email_field: str,
        phone_field: str,
        mode: str = "email_and_mobile",
        max_delete: int,
    ) -> int:
        table = self._safe_table(f"{self.cfg.table_prefix}leads")
        email = self._safe_ident(email_field)
        if str(mode).strip().lower() == "email_only":
            where = f"(`{email}` IS NULL OR `{email}` = '')"
        else:
            phone = self._safe_ident(phone_field)
            where = (
                f"(`{email}` IS NULL OR `{email}` = '') "
                f"AND (`{phone}` IS NULL OR `{phone}` = '')"
            )
        limit = max(0, int(max_delete))
        query = f"DELETE FROM `{table}` WHERE {where}"
        if limit > 0:
            query += f" LIMIT {limit}"
        with self._connect() as conn:
            with conn.cursor() as cur:
                affected = cur.execute(query)
        try:
            return int(affected)
        except (TypeError, ValueError):
            return 0

    def _table_has_index(self, table: str, index_name: str) -> bool:
        query = f"SHOW INDEX FROM `{table}` WHERE Key_name=%s"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (str(index_name),))
                row: Any = cur.fetchone()
        return isinstance(row, dict) and bool(row)

    def _page_hits_index_hint(self, table: str) -> str:
        if self._table_has_index(table, "idx_mcd_ph_lead_date"):
            return "FORCE INDEX (`idx_mcd_ph_lead_date`)"
        return ""

    def _existing_table_columns(self, table: str) -> set[str] | None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    return self._table_columns(cur, table)
        except Exception:
            return None

    def _messenger_messages_table(self) -> tuple[str | None, set[str]]:
        prefix = str(self.cfg.table_prefix or "")
        candidates = []
        if prefix:
            candidates.append(f"{prefix}messenger_messages")
        candidates.append("messenger_messages")
        seen: set[str] = set()
        for raw in candidates:
            table = self._safe_table(raw)
            if table in seen:
                continue
            seen.add(table)
            columns = self._existing_table_columns(table)
            if columns and {"id", "body"}.issubset(columns):
                return table, columns
        return None, set()

    @staticmethod
    def extract_page_hit_notification_hit_id(body: Any) -> int | None:
        raw = body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else str(body or "")
        if _PAGE_HIT_NOTIFICATION_CLASS not in raw and _PAGE_HIT_NOTIFICATION_CLASS_ESCAPED not in raw:
            return None
        for pattern in (_PAGE_HIT_ID_RE, _PAGE_HIT_ID_JSON_RE):
            m = pattern.search(raw)
            if not m:
                continue
            try:
                return int(m.group(1))
            except (TypeError, ValueError):
                return None
        return None

    def delete_orphan_page_hit_notifications(
        self,
        *,
        cutoff_utc: str,
        batch_size: int,
    ) -> dict[str, Any]:
        """Drop stale Doctrine messenger PageHitNotification rows with missing hits.

        This replaces the retired MCD PageHitNotificationHandler core patch with
        daemon-side cleanup. It only deletes old queue rows that explicitly carry
        a PageHitNotification payload and whose referenced page hit is absent.
        """

        message_table, message_columns = self._messenger_messages_table()
        if not message_table:
            return {"status": "skip", "reason": "messenger_messages_table_missing", "deleted": 0}

        page_hits_table = self._safe_table(f"{self.cfg.table_prefix}page_hits")
        if self._existing_table_columns(page_hits_table) is None:
            return {"status": "skip", "reason": "page_hits_table_missing", "deleted": 0}

        limit = max(1, int(batch_size))
        where = "WHERE `body` LIKE %s"
        params: list[Any] = ["%PageHitNotification%"]
        if "created_at" in message_columns:
            where += " AND `created_at` < %s"
            params.append(str(cutoff_utc))
        if "delivered_at" in message_columns:
            where += " AND `delivered_at` IS NULL"

        query = (
            f"SELECT `id`, `body` FROM `{message_table}` "
            f"{where} "
            "ORDER BY `id` ASC "
            "LIMIT %s"
        )
        params.append(limit)

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                rows = cur.fetchall() or []

        candidate_ids_by_hit: dict[int, list[int]] = {}
        invalid_message_ids: list[int] = []
        scanned = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            scanned += 1
            try:
                message_id = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            hit_id = self.extract_page_hit_notification_hit_id(row.get("body"))
            if hit_id is None:
                continue
            if hit_id <= 0:
                invalid_message_ids.append(message_id)
                continue
            candidate_ids_by_hit.setdefault(hit_id, []).append(message_id)

        existing_hit_ids: set[int] = set()
        hit_ids = sorted(candidate_ids_by_hit)
        if hit_ids:
            placeholders = ",".join(["%s"] * len(hit_ids))
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT `id` FROM `{page_hits_table}` WHERE `id` IN ({placeholders})", tuple(hit_ids))
                    for row in cur.fetchall() or []:
                        if not isinstance(row, dict):
                            continue
                        try:
                            existing_hit_ids.add(int(row.get("id")))
                        except (TypeError, ValueError):
                            pass

        orphan_message_ids = list(invalid_message_ids)
        for hit_id, message_ids in candidate_ids_by_hit.items():
            if hit_id not in existing_hit_ids:
                orphan_message_ids.extend(message_ids)

        if not orphan_message_ids:
            return {
                "status": "ok",
                "table": message_table,
                "scanned": scanned,
                "deleted": 0,
                "orphan_hits": 0,
                "invalid_hits": len(invalid_message_ids),
            }

        unique_message_ids = sorted(set(orphan_message_ids))
        placeholders = ",".join(["%s"] * len(unique_message_ids))
        with self._connect() as conn:
            with conn.cursor() as cur:
                deleted = int(
                    cur.execute(
                        f"DELETE FROM `{message_table}` WHERE `id` IN ({placeholders})",
                        tuple(unique_message_ids),
                    )
                    or 0
                )

        return {
            "status": "ok",
            "table": message_table,
            "scanned": scanned,
            "deleted": deleted,
            "orphan_hits": len([hit_id for hit_id in hit_ids if hit_id not in existing_hit_ids]),
            "invalid_hits": len(invalid_message_ids),
        }

    def preview_orphan_page_hits_batch(
        self,
        *,
        cutoff_utc: str,
        batch_size: int,
    ) -> dict[str, Any]:
        table = self._safe_table(f"{self.cfg.table_prefix}page_hits")
        limit = max(1, int(batch_size))
        hint = self._page_hits_index_hint(table)
        query = (
            "SELECT "
            " COUNT(*) AS preview_count, "
            " MIN(batch.id) AS min_id, "
            " MAX(batch.id) AS max_id, "
            " MIN(batch.date_hit) AS min_date_hit, "
            " MAX(batch.date_hit) AS max_date_hit "
            "FROM ("
            f" SELECT `id`, `date_hit` FROM `{table}` {hint} "
            " WHERE `lead_id` IS NULL "
            "   AND (`date_hit` IS NULL OR `date_hit` < %s) "
            " ORDER BY `date_hit` ASC, `id` ASC "
            " LIMIT %s"
            ") batch"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (str(cutoff_utc), limit))
                row: Any = cur.fetchone()
        if not isinstance(row, dict):
            return {
                "preview_count": 0,
                "min_id": None,
                "max_id": None,
                "min_date_hit": None,
                "max_date_hit": None,
            }
        return {
            "preview_count": int(row.get("preview_count") or 0),
            "min_id": row.get("min_id"),
            "max_id": row.get("max_id"),
            "min_date_hit": row.get("min_date_hit"),
            "max_date_hit": row.get("max_date_hit"),
        }

    def delete_orphan_page_hits(
        self,
        *,
        cutoff_utc: str,
        batch_size: int,
        max_batches: int,
        sleep_sec: float,
        max_run_sec: int,
    ) -> dict[str, Any]:
        table = self._safe_table(f"{self.cfg.table_prefix}page_hits")
        limit = max(1, int(batch_size))
        batches_left = max(1, int(max_batches))
        pause_sec = max(0.0, float(sleep_sec))
        run_budget_sec = max(1, int(max_run_sec))
        hint = self._page_hits_index_hint(table)
        delete_sql = (
            f"DELETE FROM `{table}` "
            "WHERE `id` IN ("
            "  SELECT doomed.id FROM ("
            f"    SELECT `id` FROM `{table}` {hint} "
            "    WHERE `lead_id` IS NULL "
            "      AND (`date_hit` IS NULL OR `date_hit` < %s) "
            "    ORDER BY `date_hit` ASC, `id` ASC "
            "    LIMIT %s"
            "  ) doomed"
            ")"
        )
        started = time.monotonic()
        total_deleted = 0
        batches_run = 0
        last_deleted = 0
        stop_reason = "empty"
        with self._connect() as conn:
            while batches_run < batches_left:
                elapsed = float(time.monotonic() - started)
                if elapsed >= run_budget_sec:
                    stop_reason = "max_run_sec"
                    break
                with conn.cursor() as cur:
                    affected = int(cur.execute(delete_sql, (str(cutoff_utc), limit)) or 0)
                last_deleted = affected
                if affected <= 0:
                    stop_reason = "empty"
                    break
                total_deleted += affected
                batches_run += 1
                if batches_run >= batches_left:
                    stop_reason = "max_batches"
                    break
                if pause_sec > 0:
                    time.sleep(pause_sec)
        return {
            "batches_run": batches_run,
            "total_deleted": total_deleted,
            "last_deleted": last_deleted,
            "elapsed_sec": max(0.0, float(time.monotonic() - started)),
            "stop_reason": stop_reason,
            "cutoff_utc": str(cutoff_utc),
            "batch_size": limit,
        }

    def delete_empty_leads(
        self,
        *,
        mode: str,
        batch_size: int,
        max_batches: int,
    ) -> dict[str, Any]:
        """
        Delete contacts that are unusable for import/matching.

        Supported modes are intentionally narrow and mirror MCC UI choices.
        Empty means NULL or an empty string; Mautic installations commonly use
        both representations for unusable contact values.
        """
        table = self._safe_table(f"{self.cfg.table_prefix}leads")
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode == "email_or_mobile_null":
            normalized_mode = "both_null"
        email_empty = "(`email` IS NULL OR `email` = '')"
        mobile_empty = "(`mobile` IS NULL OR `mobile` = '')"
        if normalized_mode == "both_null":
            predicate = f"{email_empty} AND {mobile_empty}"
        elif normalized_mode == "email_null":
            predicate = email_empty
        elif normalized_mode == "mobile_null":
            predicate = mobile_empty
        else:
            raise ValueError(f"unsupported_empty_leads_cleanup_mode:{normalized_mode}")
        limit = max(1, int(batch_size))
        batches_left = max(1, int(max_batches))
        delete_sql = (
            f"DELETE FROM `{table}` "
            "WHERE `id` IN ("
            "  SELECT doomed.id FROM ("
            f"    SELECT `id` FROM `{table}` "
            f"    WHERE {predicate} "
            "    ORDER BY `id` ASC "
            "    LIMIT %s"
            "  ) doomed"
            ")"
        )
        started = time.monotonic()
        total_deleted = 0
        batches_run = 0
        last_deleted = 0
        stop_reason = "empty"
        with self._connect() as conn:
            while batches_run < batches_left:
                with conn.cursor() as cur:
                    affected = int(cur.execute(delete_sql, (limit,)) or 0)
                last_deleted = affected
                if affected <= 0:
                    stop_reason = "empty"
                    break
                total_deleted += affected
                batches_run += 1
                if batches_run >= batches_left:
                    stop_reason = "max_batches"
                    break
        return {
            "mode": normalized_mode,
            "batches_run": batches_run,
            "total_deleted": total_deleted,
            "last_deleted": last_deleted,
            "elapsed_sec": max(0.0, float(time.monotonic() - started)),
            "stop_reason": stop_reason,
            "batch_size": limit,
        }

    def add_dnc_for_emails(self, emails: list[str], *, reason: int, comments: str) -> dict[str, Any]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in emails:
            email = str(raw or "").strip().lower()
            if not email or "@" not in email or email in seen:
                continue
            seen.add(email)
            cleaned.append(email)
        if not cleaned:
            return {"emails": 0, "contacts": 0, "added": 0, "existing": 0}

        leads_table = self._safe_table(f"{self.cfg.table_prefix}leads")
        dnc_table = self._safe_table(f"{self.cfg.table_prefix}lead_donotcontact")
        placeholders = ",".join(["%s"] * len(cleaned))
        select_leads = (
            f"SELECT `id`, LOWER(`email`) AS email "
            f"FROM `{leads_table}` "
            f"WHERE LOWER(`email`) IN ({placeholders})"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(select_leads, tuple(cleaned))
                rows = cur.fetchall() or []
                lead_ids: list[int] = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        lead_id = int(row.get("id") or 0)
                    except Exception:
                        continue
                    if lead_id > 0:
                        lead_ids.append(lead_id)
                lead_ids = list(dict.fromkeys(lead_ids))
                if not lead_ids:
                    return {"emails": len(cleaned), "contacts": 0, "added": 0, "existing": 0}

                lead_placeholders = ",".join(["%s"] * len(lead_ids))
                cur.execute(
                    f"SELECT `lead_id` FROM `{dnc_table}` "
                    f"WHERE `channel`='email' AND `lead_id` IN ({lead_placeholders})",
                    tuple(lead_ids),
                )
                existing_rows = cur.fetchall() or []
                existing_ids: set[int] = set()
                for row in existing_rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        existing_ids.add(int(row.get("lead_id") or 0))
                    except Exception:
                        continue
                to_add = [lead_id for lead_id in lead_ids if lead_id not in existing_ids]
                if to_add:
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    cur.executemany(
                        f"INSERT INTO `{dnc_table}` "
                        "(`lead_id`, `date_added`, `reason`, `channel`, `channel_id`, `comments`) "
                        "VALUES (%s, %s, %s, 'email', NULL, %s)",
                        [(lead_id, now, int(reason), str(comments or "")) for lead_id in to_add],
                    )
                return {
                    "emails": len(cleaned),
                    "contacts": len(lead_ids),
                    "added": len(to_add),
                    "existing": len(existing_ids),
                }

    def remove_email_dnc_for_emails(self, emails: list[str]) -> dict[str, Any]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in emails:
            email = str(raw or "").strip().lower()
            if not email or "@" not in email or email in seen:
                continue
            seen.add(email)
            cleaned.append(email)
        if not cleaned:
            return {"emails": 0, "contacts": 0, "removed": 0}

        leads_table = self._safe_table(f"{self.cfg.table_prefix}leads")
        dnc_table = self._safe_table(f"{self.cfg.table_prefix}lead_donotcontact")
        placeholders = ",".join(["%s"] * len(cleaned))
        select_leads = (
            f"SELECT `id` "
            f"FROM `{leads_table}` "
            f"WHERE LOWER(`email`) IN ({placeholders})"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(select_leads, tuple(cleaned))
                rows = cur.fetchall() or []
                lead_ids: list[int] = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        lead_id = int(row.get("id") or 0)
                    except Exception:
                        continue
                    if lead_id > 0:
                        lead_ids.append(lead_id)
                lead_ids = list(dict.fromkeys(lead_ids))
                if not lead_ids:
                    return {"emails": len(cleaned), "contacts": 0, "removed": 0}
                lead_placeholders = ",".join(["%s"] * len(lead_ids))
                cur.execute(
                    f"DELETE FROM `{dnc_table}` "
                    f"WHERE `channel`='email' AND `lead_id` IN ({lead_placeholders})",
                    tuple(lead_ids),
                )
                return {
                    "emails": len(cleaned),
                    "contacts": len(lead_ids),
                    "removed": int(cur.rowcount or 0),
                }

    def fetch_processlist(self, *, limit: int = 500) -> list[dict[str, Any]]:
        query = "SHOW FULL PROCESSLIST"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                out.append(row)
            if len(out) >= max(1, int(limit)):
                break
        return out

    def kill_query(self, process_id: int) -> None:
        pid = int(process_id)
        if pid <= 0:
            raise ValueError("invalid_mysql_process_id")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"KILL QUERY {pid}")

    def kill_connection(self, process_id: int) -> None:
        pid = int(process_id)
        if pid <= 0:
            raise ValueError("invalid_mysql_process_id")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"KILL CONNECTION {pid}")
