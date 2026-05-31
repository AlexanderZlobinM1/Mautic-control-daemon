from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from mcd_agent.config import AgentConfig
from mcd_agent.host_identity import resolve_agent_identity


_SCHEMA_READY: set[str] = set()
_DB_READY: set[str] = set()
_LOCAL_SOCKET_CANDIDATES: tuple[str, ...] = (
    "/var/run/mysqld/mysqld.sock",
    "/run/mysqld/mysqld.sock",
)
_MYSQL_BACKOFF: dict[str, dict[str, Any]] = {}
_MYSQL_BACKOFF_BASE_SEC = 15
_MYSQL_BACKOFF_MAX_SEC = 900
_MYSQL_BACKOFF_READONLY_SEC = 600
_MYSQL_BACKOFF_TOOMANY_SEC = 120
_MYSQL_BACKOFF_AUTH_SEC = 300
MYSQL_STATE_SCHEMA_VERSION = 2


def _backoff_key(cfg: AgentConfig) -> str:
    try:
        return _schema_key(cfg)
    except Exception:
        host = str(getattr(cfg, "state_mysql_host", "") or "").strip()
        port = int(getattr(cfg, "state_mysql_port", 3306) or 3306)
        db = str(getattr(cfg, "state_mysql_database", "") or "mcd_state").strip()
        pref = str(getattr(cfg, "state_mysql_table_prefix", "") or "mcd_").strip()
        return f"{host}|{port}|{db}|{pref}"


def _utc_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mysql_error_code(exc: Exception) -> int | None:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        args = getattr(cur, "args", ())
        if args:
            try:
                return int(args[0])
            except Exception:
                pass
        nxt = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
        cur = nxt if isinstance(nxt, BaseException) else None
    return None


def _mysql_backoff_status(cfg: AgentConfig) -> tuple[bool, dict[str, Any] | None]:
    key = _backoff_key(cfg)
    row = _MYSQL_BACKOFF.get(key)
    if not isinstance(row, dict):
        return False, None
    until_ts = float(row.get("until_ts") or 0.0)
    now = time.time()
    if until_ts <= now:
        out = dict(row)
        out["retry_after_sec"] = 0
        out["retry_after_utc"] = _utc_from_ts(until_ts) if until_ts > 0 else ""
        return False, out
    out = dict(row)
    out["retry_after_sec"] = int(max(1, until_ts - now))
    out["retry_after_utc"] = _utc_from_ts(until_ts)
    return True, out


def _mysql_backoff_clear(cfg: AgentConfig) -> None:
    _MYSQL_BACKOFF.pop(_backoff_key(cfg), None)


def _mysql_backoff_set(cfg: AgentConfig, err: Exception | str) -> None:
    key = _backoff_key(cfg)
    prev = _MYSQL_BACKOFF.get(key, {})
    failures = int(prev.get("failures") or 0) + 1
    code: int | None = None
    err_txt = str(err)
    if isinstance(err, Exception):
        code = _mysql_error_code(err)
    base = _MYSQL_BACKOFF_BASE_SEC
    if code == 1290:
        base = _MYSQL_BACKOFF_READONLY_SEC
    elif code == 1040:
        base = _MYSQL_BACKOFF_TOOMANY_SEC
    elif code in {1045, 1698}:
        base = _MYSQL_BACKOFF_AUTH_SEC
    delay = min(_MYSQL_BACKOFF_MAX_SEC, int(base * (2 ** max(0, failures - 1))))
    until_ts = time.time() + float(delay)
    _MYSQL_BACKOFF[key] = {
        "failures": failures,
        "last_error": err_txt[:500],
        "last_error_code": code,
        "until_ts": until_ts,
    }


def _mysql_preflight(cfg: AgentConfig) -> tuple[bool, str]:
    active, st = _mysql_backoff_status(cfg)
    if not active or not isinstance(st, dict):
        return True, "ok"
    retry_sec = int(st.get("retry_after_sec") or 0)
    last_err = str(st.get("last_error") or "").strip()
    if last_err:
        return False, f"mysql_backoff_active:{retry_sec}s:{last_err}"
    return False, f"mysql_backoff_active:{retry_sec}s"


def _mask_scalar(value: Any) -> Any:
    if value is None:
        return None
    raw = str(value)
    if not raw:
        return ""
    return "***"


def _sanitize_snapshot_payload(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def walk(value: Any, parent: str = "", key: str = "") -> Any:
        if isinstance(value, dict):
            nested: dict[str, Any] = {}
            for k, v in value.items():
                sk = str(k)
                lk = sk.lower()
                if parent == "config_state" and lk == "toml":
                    nested[sk] = "[omitted]"
                    continue
                if lk in {"password", "token", "secret"} or lk.endswith("_password") or lk.endswith("_token"):
                    nested[sk] = _mask_scalar(v)
                    continue
                nested[sk] = walk(v, parent=sk, key=sk)
            return nested
        if isinstance(value, list):
            return [walk(v, parent=parent, key=key) for v in value]
        return value

    for k, v in raw.items():
        out[str(k)] = walk(v, parent=str(k), key=str(k))
    return out


def normalized_state_backend(cfg: AgentConfig) -> str:
    raw = str(getattr(cfg, "state_backend", "sqlite") or "sqlite").strip().lower()
    if raw in {"mysql", "mariadb", "mysql_hybrid", "hybrid"}:
        return "mysql_hybrid"
    return "sqlite"


def _is_local_mysql_host(host: str | None) -> bool:
    raw = str(host or "").strip().lower()
    return raw in {"", "localhost", "127.0.0.1", "::1"}


def _resolved_mysql_unix_socket(cfg: AgentConfig, *, host: str | None, password: str | None) -> str | None:
    explicit = str(getattr(cfg, "state_mysql_unix_socket", "") or "").strip()
    if explicit:
        return explicit
    if not _is_local_mysql_host(host):
        return None
    if str(password or "") != "":
        return None
    for cand in _LOCAL_SOCKET_CANDIDATES:
        if os.path.exists(cand):
            return cand
    return None


def _mysql_runtime_credentials_usable(cfg: AgentConfig) -> tuple[bool, str, str | None]:
    if normalized_state_backend(cfg) != "mysql_hybrid":
        return False, "mysql_state_disabled", None
    host = str(getattr(cfg, "state_mysql_host", "") or "").strip()
    user = str(getattr(cfg, "state_mysql_user", "") or "").strip()
    pwd = str(getattr(cfg, "state_mysql_password", "") or "")
    sock = _resolved_mysql_unix_socket(cfg, host=host, password=pwd)
    if not (host or sock):
        return False, "mysql_host_missing", sock
    if not user:
        return False, "mysql_user_missing", sock
    if user.lower() == "root" and pwd == "":
        return False, "mysql_root_without_password_disabled", sock
    return True, "ok", sock


def _resolved_local_unix_socket_auto(host: str | None) -> str | None:
    if not _is_local_mysql_host(host):
        return None
    for cand in _LOCAL_SOCKET_CANDIDATES:
        if os.path.exists(cand):
            return cand
    return None


def _mysql_cli_bin() -> str | None:
    for name in ("mariadb", "mysql"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _sql_escape_lit(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "''")


def _sql_escape_ident(value: str) -> str:
    return str(value).replace("`", "``")


def _is_latin1_encode_error(exc: Exception) -> bool:
    txt = str(exc or "").lower()
    return "latin-1" in txt and "can't encode" in txt


def _run_mysql_admin_sql_cli(
    *,
    sql: str,
    admin_user: str,
    admin_password: str,
    admin_host: str,
    admin_port: int,
    admin_unix_socket: str | None,
    timeout_sec: int = 20,
) -> tuple[bool, str]:
    mysql_bin = _mysql_cli_bin()
    if not mysql_bin:
        return False, "mysql_cli_not_found"

    cmd: list[str] = [mysql_bin, "--batch", "--skip-column-names", "-u", str(admin_user or "").strip()]
    if admin_unix_socket:
        cmd.extend(["--protocol=socket", "--socket", str(admin_unix_socket)])
    else:
        cmd.extend(["--protocol=tcp", "-h", str(admin_host or "").strip() or "localhost", "-P", str(int(admin_port or 3306))])
    cmd.extend(["-e", sql])

    env = dict(os.environ)
    if str(admin_password or "") != "":
        env["MYSQL_PWD"] = str(admin_password)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=max(5, int(timeout_sec)),
            env=env,
        )
    except Exception as e:
        return False, str(e)

    if proc.returncode == 0:
        return True, (proc.stdout or "").strip()
    return False, ((proc.stderr or proc.stdout) or "").strip() or f"mysql_cli_failed_rc_{proc.returncode}"


def _safe_db_name(raw: str | None) -> str:
    name = str(raw or "").strip() or "mcd_state"
    if not re.match(r"^[A-Za-z0-9_]+$", name):
        raise ValueError(f"unsafe state mysql database name: {name!r}")
    return name


def _state_database_name(cfg: AgentConfig) -> str:
    return _safe_db_name(getattr(cfg, "state_mysql_database", ""))


def mysql_state_enabled(cfg: AgentConfig) -> bool:
    ok, _reason, _sock = _mysql_runtime_credentials_usable(cfg)
    return ok


def _safe_prefix(raw: str | None) -> str:
    prefix = str(raw or "mcd_").strip() or "mcd_"
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", prefix):
        raise ValueError(f"unsafe state mysql table prefix: {prefix!r}")
    if not prefix.endswith("_"):
        prefix = prefix + "_"
    return prefix


def _host_name_for_state(cfg: AgentConfig) -> str:
    ident = resolve_agent_identity(cfg)
    for key in (
        "local_hostname",
        "effective_hostname",
        "configured_host_name",
        "effective_mcc_host_name",
    ):
        raw = str(ident.get(key) or "").strip()
        if raw:
            return raw
    return "unknown-host"


def _mysql_conn(cfg: AgentConfig, *, include_database: bool = True) -> pymysql.connections.Connection:
    host = str(getattr(cfg, "state_mysql_host", "") or "").strip()
    password = str(getattr(cfg, "state_mysql_password", "") or "")
    unix_socket = _resolved_mysql_unix_socket(cfg, host=host, password=password)
    kwargs: dict[str, Any] = {
        "user": str(getattr(cfg, "state_mysql_user", "") or "").strip(),
        "password": password,
        "charset": "utf8mb4",
        "autocommit": True,
        "cursorclass": DictCursor,
        "connect_timeout": max(1, int(getattr(cfg, "state_mysql_connect_timeout_sec", 5) or 5)),
        "read_timeout": max(1, int(getattr(cfg, "state_mysql_read_timeout_sec", 15) or 15)),
        "write_timeout": max(1, int(getattr(cfg, "state_mysql_write_timeout_sec", 15) or 15)),
    }
    if unix_socket:
        kwargs["unix_socket"] = unix_socket
    else:
        kwargs["host"] = host
        kwargs["port"] = int(getattr(cfg, "state_mysql_port", 3306) or 3306)
    if include_database:
        kwargs["database"] = _state_database_name(cfg)
    return pymysql.connect(**kwargs)


def _table_name_map(cfg: AgentConfig) -> dict[str, str]:
    prefix = _safe_prefix(getattr(cfg, "state_mysql_table_prefix", "mcd_"))
    return {
        "schema_version": f"{prefix}schema_version",
        "outbound_events": f"{prefix}outbound_events",
        "agent_state_snapshots": f"{prefix}agent_state_snapshots",
        "tasks": f"{prefix}tasks",
        "weight_cache": f"{prefix}weight_cache",
        "runtime_sync": f"{prefix}runtime_sync",
        "manual_requests": f"{prefix}manual_requests",
    }


def _table_names(cfg: AgentConfig) -> tuple[str, str]:
    names = _table_name_map(cfg)
    return names["outbound_events"], names["agent_state_snapshots"]


def _schema_key(cfg: AgentConfig) -> str:
    host = str(getattr(cfg, "state_mysql_host", "") or "").strip()
    pwd = str(getattr(cfg, "state_mysql_password", "") or "")
    sock = _resolved_mysql_unix_socket(cfg, host=host, password=pwd)
    return "|".join(
        [
            host,
            str(int(getattr(cfg, "state_mysql_port", 3306) or 3306)),
            str(sock or ""),
            _state_database_name(cfg),
            _safe_prefix(getattr(cfg, "state_mysql_table_prefix", "mcd_")),
        ]
    )


def _db_key(cfg: AgentConfig) -> str:
    host = str(getattr(cfg, "state_mysql_host", "") or "").strip()
    pwd = str(getattr(cfg, "state_mysql_password", "") or "")
    sock = _resolved_mysql_unix_socket(cfg, host=host, password=pwd)
    return "|".join(
        [
            host,
            str(int(getattr(cfg, "state_mysql_port", 3306) or 3306)),
            str(sock or ""),
            _state_database_name(cfg).lower(),
        ]
    )


def _ensure_mysql_database(cfg: AgentConfig) -> None:
    key = _db_key(cfg)
    if key in _DB_READY:
        return
    db_name = _state_database_name(cfg)
    with _mysql_conn(cfg, include_database=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME=%s LIMIT 1",
                (db_name,),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(f"state database missing: {db_name}; run mcd-cli state-db init")
    _DB_READY.add(key)


def _state_db_exists(cfg: AgentConfig) -> bool:
    db_name = _state_database_name(cfg)
    with _mysql_conn(cfg, include_database=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME=%s LIMIT 1",
                (db_name,),
            )
            row = cur.fetchone()
            return row is not None


def state_database_exists(cfg: AgentConfig) -> tuple[bool, str]:
    if not mysql_state_enabled(cfg):
        return False, "mysql_state_disabled"
    try:
        return bool(_state_db_exists(cfg)), "ok"
    except Exception as e:
        return False, str(e)


def state_backend_status(cfg: AgentConfig, *, probe: bool = True) -> dict[str, Any]:
    desired = normalized_state_backend(cfg)
    status: dict[str, Any] = {
        "desired_backend": desired,
        "active_backend": "sqlite",
        "mode": "legacy",
        "database": _state_database_name(cfg),
        "reason": "legacy_sqlite_mode",
    }
    if desired != "mysql_hybrid":
        return status
    host = str(getattr(cfg, "state_mysql_host", "") or "").strip()
    ok_creds, reason, sock = _mysql_runtime_credentials_usable(cfg)
    if not ok_creds:
        status["reason"] = reason
        return status
    if sock:
        status["unix_socket"] = sock
    status["reason"] = "mysql_probe_pending"
    backoff_active, backoff = _mysql_backoff_status(cfg)
    if backoff_active and isinstance(backoff, dict):
        status["reason"] = "mysql_backoff_active"
        status["error"] = str(backoff.get("last_error") or "").strip()
        status["error_code"] = backoff.get("last_error_code")
        status["retry_after_sec"] = int(backoff.get("retry_after_sec") or 0)
        status["retry_after_utc"] = str(backoff.get("retry_after_utc") or "")
        return status
    if not probe:
        return status
    try:
        _ensure_mysql_database(cfg)
        with _mysql_conn(cfg) as conn:
            _ensure_mysql_schema(cfg, conn)
        _mysql_backoff_clear(cfg)
        status["active_backend"] = "mysql"
        status["mode"] = "mysql"
        status["reason"] = "ok"
        status["schema_version"] = MYSQL_STATE_SCHEMA_VERSION
        status["created_now"] = False
        return status
    except Exception as e:
        _mysql_backoff_set(cfg, e)
        _active, _backoff = _mysql_backoff_status(cfg)
        status["reason"] = "mysql_schema_unavailable"
        status["error"] = str(e)
        status["error_code"] = _mysql_error_code(e)
        if _active and isinstance(_backoff, dict):
            status["retry_after_sec"] = int(_backoff.get("retry_after_sec") or 0)
            status["retry_after_utc"] = str(_backoff.get("retry_after_utc") or "")
        status["created_now"] = False
        return status


def create_state_database_with_admin(
    cfg: AgentConfig,
    *,
    admin_user: str,
    admin_password: str | None,
    admin_host: str | None = None,
    admin_port: int | None = None,
    admin_unix_socket: str | None = None,
    runtime_user: str | None = None,
    runtime_password: str | None = None,
    runtime_database: str | None = None,
    runtime_host: str | None = None,
    runtime_port: int | None = None,
    runtime_unix_socket: str | None = None,
) -> tuple[bool, str]:
    host = str(admin_host or getattr(cfg, "state_mysql_host", "") or "localhost").strip()
    port = int(admin_port or getattr(cfg, "state_mysql_port", 3306) or 3306)
    user = str(admin_user or "").strip()
    if not user:
        return False, "admin user is required"

    db_name = _safe_db_name(runtime_database or getattr(cfg, "state_mysql_database", ""))
    rt_user = str(runtime_user or getattr(cfg, "state_mysql_user", "") or "").strip()
    rt_pwd = (
        str(runtime_password)
        if runtime_password is not None
        else str(getattr(cfg, "state_mysql_password", "") or "")
    )
    rt_host = str(runtime_host or getattr(cfg, "state_mysql_host", "") or "127.0.0.1").strip()
    rt_port = int(runtime_port or getattr(cfg, "state_mysql_port", 3306) or 3306)
    rt_socket = str(runtime_unix_socket or getattr(cfg, "state_mysql_unix_socket", "") or "").strip()
    if not rt_user:
        return False, "runtime user is required"
    if not rt_pwd:
        return False, "runtime password is required"
    if not (rt_host or rt_socket):
        return False, "runtime host/socket is required"

    try:
        admin_pwd = str(admin_password or "")
        admin_sock = str(admin_unix_socket or "").strip()
        if not admin_sock:
            # Admin bootstrap in "auto" mode should prefer local unix socket even with a non-empty password.
            admin_sock = _resolved_local_unix_socket_auto(host) or _resolved_mysql_unix_socket(
                cfg, host=host, password=admin_pwd
            )
        db_ident = _sql_escape_ident(db_name)
        rt_user_lit = _sql_escape_lit(rt_user)
        rt_pwd_lit = _sql_escape_lit(rt_pwd)
        admin_sql_statements: list[str] = [
            f"CREATE DATABASE IF NOT EXISTS `{db_ident}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
        ]
        for host_pat in ("localhost", "127.0.0.1"):
            hp_lit = _sql_escape_lit(host_pat)
            admin_sql_statements.extend(
                [
                    f"CREATE USER IF NOT EXISTS '{rt_user_lit}'@'{hp_lit}' IDENTIFIED BY '{rt_pwd_lit}'",
                    f"ALTER USER '{rt_user_lit}'@'{hp_lit}' IDENTIFIED BY '{rt_pwd_lit}'",
                    f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM '{rt_user_lit}'@'{hp_lit}'",
                    f"GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON `{db_ident}`.* TO '{rt_user_lit}'@'{hp_lit}'",
                ]
            )
        admin_sql_statements.append("FLUSH PRIVILEGES")
        admin_sql = ";\n".join(admin_sql_statements) + ";"

        admin_kwargs: dict[str, Any] = {
            "user": user,
            "password": admin_pwd,
            "charset": "utf8mb4",
            "autocommit": True,
            "cursorclass": DictCursor,
            "connect_timeout": max(1, int(getattr(cfg, "state_mysql_connect_timeout_sec", 5) or 5)),
            "read_timeout": max(1, int(getattr(cfg, "state_mysql_read_timeout_sec", 15) or 15)),
            "write_timeout": max(1, int(getattr(cfg, "state_mysql_write_timeout_sec", 15) or 15)),
        }
        if admin_sock:
            admin_kwargs["unix_socket"] = admin_sock
        else:
            admin_kwargs["host"] = host
            admin_kwargs["port"] = port
        admin_phase_err: Exception | None = None
        try:
            with pymysql.connect(**admin_kwargs) as conn:
                with conn.cursor() as cur:
                    for stmt in admin_sql_statements:
                        cur.execute(stmt)
        except Exception as e:
            admin_phase_err = e
        if admin_phase_err is not None:
            if _is_latin1_encode_error(admin_phase_err):
                ok, cli_msg = _run_mysql_admin_sql_cli(
                    sql=admin_sql,
                    admin_user=user,
                    admin_password=admin_pwd,
                    admin_host=host,
                    admin_port=port,
                    admin_unix_socket=admin_sock or None,
                    timeout_sec=max(5, int(getattr(cfg, "state_mysql_write_timeout_sec", 15) or 15)),
                )
                if not ok:
                    return False, f"admin_sql_utf8_fallback_failed: {cli_msg}"
            else:
                return False, str(admin_phase_err)

        schema_kwargs = dict(admin_kwargs)
        schema_kwargs["database"] = db_name
        with pymysql.connect(**schema_kwargs) as conn_schema:
            _install_mysql_schema(cfg, conn_schema)

        # Validate runtime account can use the prepared DB/schema. Runtime
        # daemon must never perform DDL against Galera.
        runtime_kwargs: dict[str, Any] = {
            "user": rt_user,
            "password": rt_pwd,
            "charset": "utf8mb4",
            "autocommit": True,
            "cursorclass": DictCursor,
            "connect_timeout": max(1, int(getattr(cfg, "state_mysql_connect_timeout_sec", 5) or 5)),
            "read_timeout": max(1, int(getattr(cfg, "state_mysql_read_timeout_sec", 15) or 15)),
            "write_timeout": max(1, int(getattr(cfg, "state_mysql_write_timeout_sec", 15) or 15)),
            "database": db_name,
        }
        if rt_socket:
            runtime_kwargs["unix_socket"] = rt_socket
        else:
            runtime_kwargs["host"] = rt_host
            runtime_kwargs["port"] = rt_port
        with pymysql.connect(**runtime_kwargs) as conn2:
            _ensure_mysql_schema(cfg, conn2)
        _mysql_backoff_clear(cfg)
        return True, f"state database ready: {db_name} (runtime user: {rt_user})"
    except Exception as e:
        return False, str(e)


def _ensure_mysql_schema(cfg: AgentConfig, conn: pymysql.connections.Connection) -> None:
    """
    Validate that the state schema already exists.

    This intentionally performs no DDL. In Galera/PXC cluster mode the daemon
    must not create or migrate tables during normal runtime; schema creation is
    restricted to explicit install/bootstrap commands.
    """
    key = _schema_key(cfg)
    if key in _SCHEMA_READY:
        return

    names = _table_name_map(cfg)
    required_columns: dict[str, set[str]] = {
        names["schema_version"]: {"id", "schema_version", "updated_at"},
        names["outbound_events"]: {"id", "host_name", "event_id", "event_type", "payload_json", "status"},
        names["agent_state_snapshots"]: {"id", "host_name", "payload_hash", "payload_json"},
        names["tasks"]: {"id", "host_name", "root", "task_key", "task_type", "pid", "state"},
        names["weight_cache"]: {"host_name", "kind", "root", "entity_id", "weight", "computed_at"},
        names["runtime_sync"]: {"host_name", "key", "payload_json", "updated_at"},
        names["manual_requests"]: {"id", "host_name", "root", "task_type", "command_str", "status"},
    }
    with conn.cursor() as cur:
        cur.execute("SELECT DATABASE() AS db")
        db_row = cur.fetchone() or {}
        db_name = str(db_row.get("db") or "")
        if not db_name:
            raise RuntimeError("state schema check failed: no selected database")
        for table, columns in required_columns.items():
            cur.execute(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
                """,
                (db_name, table),
            )
            present = {str(r.get("COLUMN_NAME") or "") for r in (cur.fetchall() or []) if isinstance(r, dict)}
            missing = sorted(c for c in columns if c not in present)
            if missing:
                raise RuntimeError(f"state schema not initialized: table={table} missing={','.join(missing)}")
        cur.execute(f"SELECT schema_version FROM `{names['schema_version']}` WHERE id=1 LIMIT 1")
        row = cur.fetchone()
        if not isinstance(row, dict) or row.get("schema_version") is None:
            raise RuntimeError("state schema version missing; run mcd-cli state-db init")
        try:
            version = int(row.get("schema_version") or 0)
        except Exception:
            version = 0
        if version != MYSQL_STATE_SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported state schema version: {version}; expected {MYSQL_STATE_SCHEMA_VERSION}"
            )
    _SCHEMA_READY.add(key)


def _install_mysql_schema(cfg: AgentConfig, conn: pymysql.connections.Connection) -> None:
    key = _schema_key(cfg)
    # Explicit bootstrap/install path: do not trust the runtime validation cache.
    _SCHEMA_READY.discard(key)

    names = _table_name_map(cfg)
    schema_version_table = names["schema_version"]
    events_table = names["outbound_events"]
    snapshots_table = names["agent_state_snapshots"]
    tasks_table = names["tasks"]
    weight_cache_table = names["weight_cache"]
    runtime_sync_table = names["runtime_sync"]
    manual_requests_table = names["manual_requests"]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{schema_version_table}` (
              id TINYINT PRIMARY KEY,
              schema_version INT NOT NULL,
              updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{events_table}` (
              id BIGINT PRIMARY KEY AUTO_INCREMENT,
              host_name VARCHAR(191) NOT NULL,
              event_id VARCHAR(64) NOT NULL,
              event_type VARCHAR(64) NOT NULL,
              payload_json LONGTEXT NOT NULL,
              status VARCHAR(16) NOT NULL DEFAULT 'pending',
              try_count INT NOT NULL DEFAULT 0,
              last_try_at DOUBLE NULL,
              created_at DOUBLE NOT NULL,
              sent_at DOUBLE NULL,
              last_error TEXT NULL,
              updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              UNIQUE KEY uq_host_event (host_name, event_id),
              KEY idx_host_type_status_created (host_name, event_type, status, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{snapshots_table}` (
              id BIGINT PRIMARY KEY AUTO_INCREMENT,
              host_name VARCHAR(191) NOT NULL,
              payload_hash VARCHAR(64) NOT NULL,
              payload_json LONGTEXT NOT NULL,
              profile_name VARCHAR(64) NULL,
              instances_count INT NOT NULL DEFAULT 0,
              push_status VARCHAR(32) NULL,
              push_error TEXT NULL,
              created_at DOUBLE NOT NULL,
              updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              UNIQUE KEY uq_host_state (host_name),
              KEY idx_payload_hash (payload_hash)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{tasks_table}` (
              id BIGINT PRIMARY KEY AUTO_INCREMENT,
              host_name VARCHAR(191) NOT NULL,
              root VARCHAR(512) NOT NULL,
              task_key VARCHAR(768) NOT NULL,
              task_type VARCHAR(64) NOT NULL,
              entity_id BIGINT NULL,
              command_str LONGTEXT NOT NULL,
              pid INT NOT NULL,
              timeout_sec INT NOT NULL,
              attempts INT NOT NULL DEFAULT 1,
              manual_request_id BIGINT NULL,
              state VARCHAR(32) NOT NULL,
              note TEXT NULL,
              started_at DOUBLE NOT NULL,
              finished_at DOUBLE NULL,
              rc INT NULL,
              KEY idx_tasks_running (host_name, state, root(191), task_type, started_at),
              KEY idx_tasks_key (host_name, task_key(191))
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{weight_cache_table}` (
              host_name VARCHAR(191) NOT NULL,
              kind VARCHAR(32) NOT NULL,
              root VARCHAR(512) NOT NULL,
              entity_id BIGINT NOT NULL,
              weight DOUBLE NOT NULL,
              computed_at DOUBLE NOT NULL,
              PRIMARY KEY (host_name, kind, root(191), entity_id),
              KEY idx_weight_cache_lookup (host_name, kind, root(191), computed_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{runtime_sync_table}` (
              host_name VARCHAR(191) NOT NULL,
              `key` VARCHAR(191) NOT NULL,
              payload_json LONGTEXT NOT NULL,
              updated_at DOUBLE NOT NULL,
              PRIMARY KEY (host_name, `key`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{manual_requests_table}` (
              id BIGINT PRIMARY KEY AUTO_INCREMENT,
              host_name VARCHAR(191) NOT NULL,
              root VARCHAR(512) NOT NULL,
              task_type VARCHAR(64) NOT NULL,
              entity_id BIGINT NULL,
              command_str LONGTEXT NOT NULL,
              timeout_sec INT NOT NULL,
              status VARCHAR(32) NOT NULL DEFAULT 'pending',
              note TEXT NULL,
              task_key VARCHAR(768) NULL,
              requested_at DOUBLE NOT NULL,
              launched_at DOUBLE NULL,
              finished_at DOUBLE NULL,
              KEY idx_manual_requests_pending (host_name, status, root(191), requested_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )

        # Legacy -> host-scoped migration for shared mysql_hybrid state tables.
        # Goal: every node can write only its own records in cluster mode.
        legacy_host = "legacy-shared"

        def _has_column(table: str, column: str) -> bool:
            cur.execute(
                """
                SELECT 1
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s
                LIMIT 1
                """,
                (table, column),
            )
            return cur.fetchone() is not None

        def _has_index(table: str, index_name: str) -> bool:
            cur.execute(
                """
                SELECT 1
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s
                LIMIT 1
                """,
                (table, index_name),
            )
            return cur.fetchone() is not None

        def _index_columns(table: str, index_name: str) -> list[str]:
            cur.execute(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s
                ORDER BY SEQ_IN_INDEX ASC
                """,
                (table, index_name),
            )
            rows = cur.fetchall() or []
            return [str(r.get("COLUMN_NAME") or "") for r in rows if isinstance(r, dict)]

        def _rebuild_index(table: str, index_name: str, expr: str) -> None:
            if _has_index(table, index_name):
                cur.execute(f"ALTER TABLE `{table}` DROP INDEX `{index_name}`")
            cur.execute(f"ALTER TABLE `{table}` ADD INDEX `{index_name}` ({expr})")

        # tasks: add host scope + host-aware indexes
        if not _has_column(tasks_table, "host_name"):
            cur.execute(
                f"""
                ALTER TABLE `{tasks_table}`
                ADD COLUMN host_name VARCHAR(191) NOT NULL DEFAULT '{legacy_host}' AFTER id
                """
            )
        if _index_columns(tasks_table, "idx_tasks_running") != ["host_name", "state", "root", "task_type", "started_at"]:
            _rebuild_index(tasks_table, "idx_tasks_running", "host_name, state, root(191), task_type, started_at")
        if _index_columns(tasks_table, "idx_tasks_key") != ["host_name", "task_key"]:
            _rebuild_index(tasks_table, "idx_tasks_key", "host_name, task_key(191)")

        # weight_cache: reset legacy shared entries and switch PK to host scope.
        # Weight cache is derived data and safe to rebuild.
        if not _has_column(weight_cache_table, "host_name"):
            cur.execute(f"TRUNCATE TABLE `{weight_cache_table}`")
            cur.execute(
                f"""
                ALTER TABLE `{weight_cache_table}`
                ADD COLUMN host_name VARCHAR(191) NOT NULL DEFAULT '{legacy_host}' FIRST
                """
            )
        if _index_columns(weight_cache_table, "PRIMARY") != ["host_name", "kind", "root", "entity_id"]:
            cur.execute(f"ALTER TABLE `{weight_cache_table}` DROP PRIMARY KEY")
            cur.execute(
                f"""
                ALTER TABLE `{weight_cache_table}`
                ADD PRIMARY KEY (host_name, kind, root(191), entity_id)
                """
            )
        if _index_columns(weight_cache_table, "idx_weight_cache_lookup") != ["host_name", "kind", "root", "computed_at"]:
            _rebuild_index(weight_cache_table, "idx_weight_cache_lookup", "host_name, kind, root(191), computed_at")

        # runtime_sync: make keys host-scoped.
        if not _has_column(runtime_sync_table, "host_name"):
            cur.execute(
                f"""
                ALTER TABLE `{runtime_sync_table}`
                ADD COLUMN host_name VARCHAR(191) NOT NULL DEFAULT '{legacy_host}' FIRST
                """
            )
        # Replace legacy PK(key) with PK(host_name,key).
        if _index_columns(runtime_sync_table, "PRIMARY") != ["host_name", "key"]:
            cur.execute(f"ALTER TABLE `{runtime_sync_table}` DROP PRIMARY KEY")
            cur.execute(
                f"""
                ALTER TABLE `{runtime_sync_table}`
                ADD PRIMARY KEY (host_name, `key`)
                """
            )

        # manual_requests: add host scope + host-aware pending index.
        if not _has_column(manual_requests_table, "host_name"):
            cur.execute(
                f"""
                ALTER TABLE `{manual_requests_table}`
                ADD COLUMN host_name VARCHAR(191) NOT NULL DEFAULT '{legacy_host}' AFTER id
                """
            )
        if _index_columns(manual_requests_table, "idx_manual_requests_pending") != ["host_name", "status", "root", "requested_at"]:
            _rebuild_index(manual_requests_table, "idx_manual_requests_pending", "host_name, status, root(191), requested_at")
        cur.execute(
            f"""
            INSERT INTO `{schema_version_table}`(id, schema_version)
            VALUES(1, %s)
            ON DUPLICATE KEY UPDATE schema_version=VALUES(schema_version)
            """,
            (MYSQL_STATE_SCHEMA_VERSION,),
        )
    # Do not mark runtime schema as validated here: install may run with admin
    # credentials, but the daemon uses the restricted runtime account.
    _SCHEMA_READY.discard(key)


def ensure_mysql_state_schema(cfg: AgentConfig) -> None:
    """Validate that state MySQL DB/tables are present and supported."""
    if not mysql_state_enabled(cfg):
        return
    _ensure_mysql_database(cfg)
    with _mysql_conn(cfg) as conn:
        _ensure_mysql_schema(cfg, conn)


def queue_outbound_event_mysql(
    cfg: AgentConfig,
    *,
    event_type: str,
    event_id: str,
    payload_json: str,
    created_at: float,
) -> tuple[bool, str]:
    if not mysql_state_enabled(cfg):
        return False, "mysql_state_disabled"
    ok_preflight, preflight_msg = _mysql_preflight(cfg)
    if not ok_preflight:
        return False, preflight_msg
    host_name = _host_name_for_state(cfg)
    events_table, _ = _table_names(cfg)
    try:
        _ensure_mysql_database(cfg)
        with _mysql_conn(cfg) as conn:
            _ensure_mysql_schema(cfg, conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO `{events_table}`
                    (host_name, event_id, event_type, payload_json, status, try_count, created_at)
                    VALUES (%s, %s, %s, %s, 'pending', 0, %s)
                    ON DUPLICATE KEY UPDATE
                      event_type=VALUES(event_type),
                      payload_json=VALUES(payload_json),
                      status='pending',
                      last_error=NULL
                    """,
                    (host_name, event_id, event_type, payload_json, float(created_at)),
                )
        _mysql_backoff_clear(cfg)
        return True, "ok"
    except Exception as e:
        _mysql_backoff_set(cfg, e)
        return False, str(e)


def read_pending_outbound_event_mysql(cfg: AgentConfig, *, event_type: str) -> tuple[dict[str, Any] | None, str]:
    if not mysql_state_enabled(cfg):
        return None, "mysql_state_disabled"
    ok_preflight, preflight_msg = _mysql_preflight(cfg)
    if not ok_preflight:
        return None, preflight_msg
    host_name = _host_name_for_state(cfg)
    events_table, _ = _table_names(cfg)
    try:
        _ensure_mysql_database(cfg)
        with _mysql_conn(cfg) as conn:
            _ensure_mysql_schema(cfg, conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT event_id, payload_json
                    FROM `{events_table}`
                    WHERE host_name=%s
                      AND event_type=%s
                      AND status IN ('pending','failed')
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (host_name, event_type),
                )
                row = cur.fetchone()
        if not isinstance(row, dict):
            return None, "empty"
        payload_json = str(row.get("payload_json") or "")
        try:
            raw = json.loads(payload_json)
        except Exception:
            raw = None
        if not isinstance(raw, dict):
            now_ts = datetime.now(timezone.utc).timestamp()
            with _mysql_conn(cfg) as conn:
                _ensure_mysql_schema(cfg, conn)
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        UPDATE `{events_table}`
                        SET status='failed', try_count=try_count+1, last_try_at=%s, last_error=%s
                        WHERE host_name=%s AND event_type=%s AND event_id=%s
                        """,
                        (
                            float(now_ts),
                            "invalid_payload",
                            host_name,
                            event_type,
                            str(row.get("event_id") or ""),
                        ),
                    )
            return None, "invalid_payload"
        _mysql_backoff_clear(cfg)
        return raw, "ok"
    except Exception as e:
        _mysql_backoff_set(cfg, e)
        return None, str(e)


def mark_outbound_event_mysql(
    cfg: AgentConfig,
    *,
    event_id: str,
    delivered: bool,
    error: str | None,
) -> tuple[bool, str]:
    if not mysql_state_enabled(cfg):
        return False, "mysql_state_disabled"
    ok_preflight, preflight_msg = _mysql_preflight(cfg)
    if not ok_preflight:
        return False, preflight_msg
    host_name = _host_name_for_state(cfg)
    now_ts = datetime.now(timezone.utc).timestamp()
    events_table, _ = _table_names(cfg)
    try:
        _ensure_mysql_database(cfg)
        with _mysql_conn(cfg) as conn:
            _ensure_mysql_schema(cfg, conn)
            with conn.cursor() as cur:
                if delivered:
                    cur.execute(
                        f"""
                        UPDATE `{events_table}`
                        SET status='sent', sent_at=%s, last_try_at=%s, last_error=NULL
                        WHERE host_name=%s AND event_id=%s
                        """,
                        (float(now_ts), float(now_ts), host_name, event_id),
                    )
                else:
                    cur.execute(
                        f"""
                        UPDATE `{events_table}`
                        SET status='failed', try_count=try_count+1, last_try_at=%s, last_error=%s
                        WHERE host_name=%s AND event_id=%s
                        """,
                        (float(now_ts), str(error or "delivery_failed")[:2000], host_name, event_id),
                    )
        _mysql_backoff_clear(cfg)
        return True, "ok"
    except Exception as e:
        _mysql_backoff_set(cfg, e)
        return False, str(e)


def prune_outbound_events_mysql(cfg: AgentConfig, *, event_type: str, cutoff_ts: float) -> tuple[int, str]:
    if not mysql_state_enabled(cfg):
        return 0, "mysql_state_disabled"
    ok_preflight, preflight_msg = _mysql_preflight(cfg)
    if not ok_preflight:
        return 0, preflight_msg
    host_name = _host_name_for_state(cfg)
    events_table, _ = _table_names(cfg)
    try:
        _ensure_mysql_database(cfg)
        with _mysql_conn(cfg) as conn:
            _ensure_mysql_schema(cfg, conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    DELETE FROM `{events_table}`
                    WHERE host_name=%s
                      AND event_type=%s
                      AND status='sent'
                      AND COALESCE(sent_at, created_at) < %s
                    """,
                    (host_name, event_type, float(cutoff_ts)),
                )
                deleted = int(cur.rowcount or 0)
        _mysql_backoff_clear(cfg)
        return deleted, "ok"
    except Exception as e:
        _mysql_backoff_set(cfg, e)
        return 0, str(e)


def upsert_state_snapshot_mysql(
    cfg: AgentConfig,
    *,
    payload: dict[str, Any],
    payload_hash: str,
    created_at: float,
) -> tuple[bool, str]:
    if not mysql_state_enabled(cfg):
        return False, "mysql_state_disabled"
    if not bool(getattr(cfg, "state_mysql_snapshot_enabled", True)):
        return False, "snapshot_disabled"
    ok_preflight, preflight_msg = _mysql_preflight(cfg)
    if not ok_preflight:
        return False, preflight_msg

    host_name = _host_name_for_state(cfg)
    _, snapshots_table = _table_names(cfg)
    try:
        _ensure_mysql_database(cfg)
        safe_payload = _sanitize_snapshot_payload(payload)
        payload_json = json.dumps(safe_payload, ensure_ascii=True, separators=(",", ":"))
        profile_name = str(payload.get("profile") or "").strip().lower() or None
        instances = payload.get("instances")
        instances_count = len(instances) if isinstance(instances, list) else 0
        with _mysql_conn(cfg) as conn:
            _ensure_mysql_schema(cfg, conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO `{snapshots_table}`
                    (host_name, payload_hash, payload_json, profile_name, instances_count, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      payload_hash=VALUES(payload_hash),
                      payload_json=VALUES(payload_json),
                      profile_name=VALUES(profile_name),
                      instances_count=VALUES(instances_count),
                      created_at=VALUES(created_at),
                      push_status=NULL,
                      push_error=NULL
                    """,
                    (host_name, payload_hash, payload_json, profile_name, int(instances_count), float(created_at)),
                )
        _mysql_backoff_clear(cfg)
        return True, "ok"
    except Exception as e:
        _mysql_backoff_set(cfg, e)
        return False, str(e)


def mark_state_snapshot_push_result_mysql(
    cfg: AgentConfig,
    *,
    payload_hash: str,
    delivered: bool,
    message: str,
) -> tuple[bool, str]:
    if not mysql_state_enabled(cfg):
        return False, "mysql_state_disabled"
    if not bool(getattr(cfg, "state_mysql_snapshot_enabled", True)):
        return False, "snapshot_disabled"
    ok_preflight, preflight_msg = _mysql_preflight(cfg)
    if not ok_preflight:
        return False, preflight_msg

    host_name = _host_name_for_state(cfg)
    _, snapshots_table = _table_names(cfg)
    status = "sent" if delivered else "failed"
    try:
        _ensure_mysql_database(cfg)
        with _mysql_conn(cfg) as conn:
            _ensure_mysql_schema(cfg, conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE `{snapshots_table}`
                    SET push_status=%s,
                        push_error=%s
                    WHERE host_name=%s AND payload_hash=%s
                    """,
                    (status, None if delivered else str(message or "")[:2000], host_name, payload_hash),
                )
        _mysql_backoff_clear(cfg)
        return True, "ok"
    except Exception as e:
        _mysql_backoff_set(cfg, e)
        return False, str(e)


def ensure_mysql_state_ready(cfg: AgentConfig) -> tuple[bool, str]:
    """Ensure state DB and schema exist and are reachable by runtime creds."""
    if not mysql_state_enabled(cfg):
        return False, "mysql_state_disabled"
    ok_preflight, preflight_msg = _mysql_preflight(cfg)
    if not ok_preflight:
        return False, preflight_msg
    try:
        _ensure_mysql_database(cfg)
        with _mysql_conn(cfg) as conn:
            _ensure_mysql_schema(cfg, conn)
        _mysql_backoff_clear(cfg)
        return True, "ok"
    except Exception as e:
        _mysql_backoff_set(cfg, e)
        return False, str(e)


def mysql_state_connection(cfg: AgentConfig) -> pymysql.connections.Connection:
    """Open a state DB connection with ensured database/schema."""
    ok, reason = ensure_mysql_state_ready(cfg)
    if not ok:
        raise RuntimeError(reason)
    return _mysql_conn(cfg)


def mysql_state_existing_connection(cfg: AgentConfig) -> pymysql.connections.Connection:
    """Open state DB without creating/upgrading database schema.

    Cluster runtime coordination must never create or alter Galera-backed state
    schema as a side effect. Use this only for code paths that require the
    state schema to exist already and should fail closed if it does not.
    """
    ok, reason = _mysql_preflight(cfg)
    if not ok:
        raise RuntimeError(reason)
    try:
        conn = _mysql_conn(cfg)
        _mysql_backoff_clear(cfg)
        return conn
    except Exception as e:
        _mysql_backoff_set(cfg, e)
        raise


def mysql_state_table_names(cfg: AgentConfig) -> dict[str, str]:
    """Return fully-qualified state table names map for current prefix."""
    return dict(_table_name_map(cfg))
