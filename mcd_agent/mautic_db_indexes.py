from __future__ import annotations

from dataclasses import dataclass
import os
import types
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from mcd_agent.config import AgentConfig
from mcd_agent.discovery import discover_mautic
from mcd_agent.models import DBConfig, MauticInstall


_LOCAL_SOCKET_CANDIDATES = (
    "/run/mysqld/mysqld.sock",
    "/var/run/mysqld/mysqld.sock",
    "/tmp/mysql.sock",
)

_PYMYSQL_ERR = getattr(pymysql, "err", None)
if _PYMYSQL_ERR is None:
    _PYMYSQL_ERR = types.SimpleNamespace(OperationalError=Exception)
    setattr(pymysql, "err", _PYMYSQL_ERR)
_PYMYSQL_OPERATIONAL_ERROR = getattr(_PYMYSQL_ERR, "OperationalError", Exception)


@dataclass(frozen=True)
class ManagedIndex:
    table: str
    name: str
    columns: tuple[str, ...]


MANAGED_INDEXES: tuple[ManagedIndex, ...] = (
    ManagedIndex(
        table="lead_lists_leads",
        name="idx_mcd_lll_list_date_removed_lead",
        columns=("leadlist_id", "date_added", "manually_removed", "lead_id"),
    ),
    ManagedIndex(
        table="lead_lists_leads",
        name="idx_mcd_lll_list_removed_date_lead",
        columns=("leadlist_id", "manually_removed", "date_added", "lead_id"),
    ),
    ManagedIndex(
        table="audit_log",
        name="idx_mcd_audit_segment_due",
        columns=("bundle", "object", "object_id", "action", "date_added"),
    ),
    ManagedIndex(
        table="leads",
        name="idx_mcd_leads_email",
        columns=("email",),
    ),
    ManagedIndex(
        table="leads",
        name="idx_mcd_leads_mobile",
        columns=("mobile",),
    ),
    ManagedIndex(
        table="campaign_lead_event_log",
        name="idx_mcd_clel_scheduled_trigger_id",
        columns=("is_scheduled", "trigger_date", "id"),
    ),
    ManagedIndex(
        table="page_hits",
        name="idx_mcd_ph_lead_date",
        columns=("lead_id", "date_hit", "id"),
    ),
)


def _quote_ident(value: str) -> str:
    return "`" + str(value).replace("`", "``") + "`"


def _connect(db: DBConfig) -> pymysql.connections.Connection:
    host = str(db.host or "").strip()
    kwargs: dict[str, Any] = {
        "port": int(db.port or 3306),
        "user": db.user,
        "password": db.password,
        "database": db.name,
        "charset": "utf8mb4",
        "autocommit": True,
        "cursorclass": DictCursor,
        "connect_timeout": 5,
        # Online index builds on large Mautic tables can legitimately run for
        # hours. Keep the client attached so completion is reported correctly
        # instead of abandoning a still-running server-side ALTER.
        "read_timeout": 7200,
        "write_timeout": 30,
    }
    if host.lower() in {"", "localhost", "127.0.0.1", "::1"}:
        sock = next((cand for cand in _LOCAL_SOCKET_CANDIDATES if os.path.exists(cand)), "")
        if sock:
            kwargs["unix_socket"] = sock
        else:
            kwargs["host"] = host or "localhost"
    else:
        kwargs["host"] = host
    return pymysql.connect(**kwargs)


def _existing_indexes(conn: pymysql.connections.Connection, *, db_name: str, table: str) -> dict[str, tuple[str, ...]]:
    sql = """
        SELECT INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
        ORDER BY INDEX_NAME, SEQ_IN_INDEX
    """
    out: dict[str, list[tuple[int, str]]] = {}
    with conn.cursor() as cur:
        cur.execute(sql, (db_name, table))
        rows = cur.fetchall()
    for row in rows:
        name = str(row.get("INDEX_NAME") or "")
        col = str(row.get("COLUMN_NAME") or "")
        seq = int(row.get("SEQ_IN_INDEX") or 0)
        if not name or not col or seq <= 0:
            continue
        out.setdefault(name, []).append((seq, col))
    return {name: tuple(col for _seq, col in sorted(cols)) for name, cols in out.items()}


def _index_already_present(existing: dict[str, tuple[str, ...]], index: ManagedIndex) -> tuple[bool, str]:
    if existing.get(index.name) == index.columns:
        return True, "name_match"
    for name, cols in existing.items():
        if cols == index.columns:
            return True, f"columns_match:{name}"
    return False, ""


def _add_index_sql(prefix: str, index: ManagedIndex) -> str:
    table = _quote_ident(f"{prefix}{index.table}")
    cols = ", ".join(_quote_ident(c) for c in index.columns)
    return (
        f"ALTER TABLE {table} ADD INDEX {_quote_ident(index.name)} ({cols}), "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )


def _drop_index_sql(table_name: str, index_name: str) -> str:
    return (
        f"ALTER TABLE {_quote_ident(table_name)} DROP INDEX {_quote_ident(index_name)}, "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )


def _is_too_many_indexes_error(exc: _PYMYSQL_OPERATIONAL_ERROR) -> bool:
    code = int(exc.args[0]) if exc.args else 0
    message = str(exc).lower()
    return code == 1069 or "too many keys" in message or "max 64 keys" in message


def _is_duplicate_key_error(exc: _PYMYSQL_OPERATIONAL_ERROR) -> bool:
    code = int(exc.args[0]) if exc.args else 0
    message = str(exc).lower()
    return code == 1061 or "duplicate key name" in message


def _fax_indexes_to_drop(existing: dict[str, tuple[str, ...]]) -> list[str]:
    out: list[str] = []
    for name, columns in existing.items():
        if name == "PRIMARY":
            continue
        lowered_name = str(name or "").lower()
        lowered_cols = tuple(str(c or "").lower() for c in columns)
        if lowered_cols == ("fax",) or "fax" in lowered_name:
            out.append(name)
    return sorted(set(out))


def apply_mautic_db_indexes_to_install(
    install: MauticInstall,
    *,
    dry_run: bool = False,
    lock_wait_timeout_sec: int = 10,
) -> dict[str, Any]:
    db = install.db
    if db is None:
        return {"status": "skipped", "reason": "db_config_missing", "root": install.root}
    prefix = str(db.table_prefix or "")
    planned: list[dict[str, Any]] = []
    applied: list[str] = []
    dropped: list[str] = []
    skipped: list[dict[str, str]] = []

    with _connect(db) as conn:
        existing_by_table: dict[str, dict[str, tuple[str, ...]]] = {}
        for idx in MANAGED_INDEXES:
            table_name = f"{prefix}{idx.table}"
            existing = existing_by_table.get(table_name)
            if existing is None:
                existing = _existing_indexes(conn, db_name=db.name, table=table_name)
                existing_by_table[table_name] = existing
            present, reason = _index_already_present(existing, idx)
            if present:
                skipped.append({"index": idx.name, "reason": reason})
                continue
            sql = _add_index_sql(prefix, idx)
            planned.append({"index": idx.name, "table": table_name, "columns": list(idx.columns), "sql": sql})
            if dry_run:
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SET SESSION lock_wait_timeout={max(1, int(lock_wait_timeout_sec))}")
                    cur.execute(f"SET SESSION innodb_lock_wait_timeout={max(1, int(lock_wait_timeout_sec))}")
                    cur.execute(sql)
                applied.append(idx.name)
            except _PYMYSQL_OPERATIONAL_ERROR as exc:
                code = int(exc.args[0]) if exc.args else 0
                if code in {1205, 1213}:
                    return {
                        "status": "deferred",
                        "reason": "table_busy",
                        "error": str(exc),
                        "applied": applied,
                        "dropped": dropped,
                        "planned": planned,
                        "skipped": skipped,
                        "root": install.root,
                    }
                if _is_duplicate_key_error(exc):
                    refreshed = _existing_indexes(conn, db_name=db.name, table=table_name)
                    existing_by_table[table_name] = refreshed
                    present, reason = _index_already_present(refreshed, idx)
                    if present:
                        skipped.append({"index": idx.name, "reason": f"already_created:{reason}"})
                        continue
                if idx.table == "leads" and _is_too_many_indexes_error(exc):
                    fax_indexes = _fax_indexes_to_drop(existing)
                    if not fax_indexes:
                        raise
                    try:
                        with conn.cursor() as cur:
                            cur.execute(f"SET SESSION lock_wait_timeout={max(1, int(lock_wait_timeout_sec))}")
                            cur.execute(f"SET SESSION innodb_lock_wait_timeout={max(1, int(lock_wait_timeout_sec))}")
                            for fax_idx in fax_indexes:
                                cur.execute(_drop_index_sql(table_name, fax_idx))
                                dropped.append(f"{table_name}.{fax_idx}")
                                existing.pop(fax_idx, None)
                            cur.execute(sql)
                        applied.append(idx.name)
                    except _PYMYSQL_OPERATIONAL_ERROR as retry_exc:
                        retry_code = int(retry_exc.args[0]) if retry_exc.args else 0
                        if retry_code in {1205, 1213}:
                            return {
                                "status": "deferred",
                                "reason": "table_busy",
                                "error": str(retry_exc),
                                "applied": applied,
                                "dropped": dropped,
                                "planned": planned,
                                "skipped": skipped,
                                "root": install.root,
                            }
                        raise
                    continue
                raise

    if dry_run:
        return {"status": "planned", "planned": planned, "skipped": skipped, "root": install.root}
    return {
        "status": "applied" if applied else "noop",
        "applied": applied,
        "dropped": dropped,
        "planned": planned,
        "skipped": skipped,
        "root": install.root,
    }


def apply_mautic_db_indexes(cfg: AgentConfig, *, dry_run: bool = False) -> dict[str, Any]:
    installs = discover_mautic(
        cfg.discovery_roots,
        cfg.exclude_path_contains,
        cfg.supported_mautic_majors,
        cfg.custom_instances,
    )
    results: list[dict[str, Any]] = []
    for inst in installs:
        results.append(apply_mautic_db_indexes_to_install(inst, dry_run=dry_run))

    statuses = {str(r.get("status") or "") for r in results}
    if not results:
        status = "skipped"
        reason = "no_mautic_instances"
    elif "deferred" in statuses:
        status = "deferred"
        reason = "one_or_more_tables_busy"
    elif dry_run:
        status = "planned"
        reason = "dry_run"
    elif statuses <= {"noop", "skipped"}:
        status = "noop"
        reason = "all_indexes_present_or_skipped"
    else:
        status = "applied"
        reason = "ok"
    return {"status": status, "reason": reason, "instances": results}
