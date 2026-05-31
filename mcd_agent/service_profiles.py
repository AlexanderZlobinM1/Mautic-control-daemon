from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from mcd_agent.apt_profile import apply_apt_profile
from mcd_agent import __version__
from mcd_agent.config import AgentConfig
from mcd_agent.host_identity import resolve_agent_identity
from mcd_agent.mautic_db_indexes import apply_mautic_db_indexes


_SYSCTL_PATH = Path("/etc/sysctl.d/99-mcd-hw.conf")
_MYSQL_TOP_LEVEL_CONFIGS = (Path("/etc/mysql/my.cnf"), Path("/etc/mysql/mysql.cnf"))
_MYSQL_PROFILE_MANAGED_KEYS = {
    "innodb_buffer_pool_size",
    "innodb_redo_log_capacity",
    "innodb_io_capacity",
    "innodb_io_capacity_max",
    "max_connections",
    "thread_cache_size",
    "table_open_cache",
    "table_definition_cache",
    "open_files_limit",
    "tmp_table_size",
    "max_heap_table_size",
    "max_allowed_packet",
    "skip_name_resolve",
    "log_queries_not_using_indexes",
}
_MYSQL_GALERA_MARKERS = (
    "wsrep_on",
    "wsrep_provider",
    "wsrep_cluster_address",
    "wsrep_cluster_name",
)


def _post_json(url: str, payload: dict[str, Any], token: str | None, timeout_sec: int = 12) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = request.Request(url=url, data=data, method="POST", headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with request.urlopen(req, timeout=timeout_sec) as resp:
        body = (resp.read() or b"").decode("utf-8", errors="replace")
    raw = json.loads(body or "{}")
    return raw if isinstance(raw, dict) else {}


def _api_base(cfg: AgentConfig) -> str | None:
    if not cfg.mcc_url:
        return None
    return cfg.mcc_url.rstrip("/")


def fetch_service_profile(cfg: AgentConfig, component: str) -> dict[str, Any]:
    base = _api_base(cfg)
    if not base:
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    comp = (component or "").strip().lower().replace("-", "_")
    if comp not in {"php_fpm", "mysql", "apt", "mautic_db_indexes", "db_indexes"}:
        return {"status": "error", "reason": f"unsupported component: {component}"}
    ident = resolve_agent_identity(cfg)
    payload = {
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "agent_version": __version__,
    }
    url = base + f"/api/v1/agent/service-profile/{comp.replace('_', '-')}"
    try:
        out = _post_json(url, payload, cfg.mcc_token, timeout_sec=12)
        out.setdefault("status", "error")
        return out
    except HTTPError as e:
        return {"status": "error", "reason": f"http_{e.code}"}
    except URLError as e:
        return {"status": "error", "reason": f"urlerror:{e.reason}"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def fetch_php_fpm_profile(cfg: AgentConfig) -> dict[str, Any]:
    return fetch_service_profile(cfg, "php_fpm")


def fetch_mysql_profile(cfg: AgentConfig) -> dict[str, Any]:
    return fetch_service_profile(cfg, "mysql")


def fetch_apt_profile(cfg: AgentConfig) -> dict[str, Any]:
    return fetch_service_profile(cfg, "apt")


def _detect_php_version() -> str:
    p = Path("/etc/php")
    if not p.exists():
        raise RuntimeError("/etc/php not found")
    versions = sorted(
        [x.name for x in p.iterdir() if x.is_dir() and (x / "fpm").is_dir() and (x / "cli").is_dir()],
        key=_php_ver_key,
    )
    if not versions:
        raise RuntimeError("no php versions with fpm+cli found in /etc/php")
    running = _detect_running_php_fpm_version()
    if running and running in versions:
        return running
    return versions[-1]


def _installed_php_versions() -> list[str]:
    p = Path("/etc/php")
    if not p.exists():
        return []
    versions = sorted(
        [x.name for x in p.iterdir() if x.is_dir() and (x / "fpm").is_dir() and (x / "cli").is_dir()],
        key=_php_ver_key,
    )
    return versions


def _find_php_fpm_bin(ver: str) -> str:
    for cand in (f"php-fpm{ver}", f"php-fpm{ver.split('.')[0]}", "php-fpm"):
        path = shutil.which(cand)
        if path:
            return path
    raise RuntimeError("php-fpm binary not found")


def _php_ver_key(ver: str) -> tuple[int, ...]:
    out: list[int] = []
    for part in str(ver).split("."):
        try:
            out.append(int(part))
        except Exception:
            out.append(0)
    return tuple(out)


def _detect_running_php_fpm_version() -> str | None:
    """
    Prefer running php-fpm version when multiple versions are installed.
    """
    try:
        proc = subprocess.run(["ps", "-eo", "comm=,args="], capture_output=True, text=True)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    rx = re.compile(r"php-fpm(?P<ver>\d+\.\d+)")
    found: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        m = rx.search(line)
        if m:
            found.add(m.group("ver"))
    if not found:
        return None
    return sorted(found, key=_php_ver_key)[-1]


def _service_exists(name: str) -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "cat", f"{name}.service"],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _detect_php_fpm_service_name(ver: str) -> str:
    candidates = [f"php{ver}-fpm", f"php{ver.split('.')[0]}-fpm", "php-fpm"]
    for svc in candidates:
        if _service_exists(svc):
            return svc
    return candidates[0]


def _write_file(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def _remove_legacy_php_ini_baseline_files() -> list[str]:
    """
    Remove historical global php baseline file that caused side effects.
    Applies to all installed PHP versions, not only active one.
    """
    removed: list[str] = []
    for ver in _installed_php_versions():
        for sap in ("fpm", "cli"):
            p = Path(f"/etc/php/{ver}/{sap}/conf.d/98-mcd-php.ini")
            if p.exists():
                try:
                    p.unlink()
                    removed.append(str(p))
                except Exception:
                    pass
    return removed


def _build_pool_override(profile: dict[str, Any]) -> str:
    pool = str(profile.get("pool", "www")).strip() or "www"
    pm = str(profile.get("pm", "dynamic")).strip() or "dynamic"
    pm_max_children = int(profile.get("pm_max_children", 16) or 16)
    pm_start_servers = int(profile.get("pm_start_servers", 4) or 4)
    pm_min_spare_servers = int(profile.get("pm_min_spare_servers", 2) or 2)
    pm_max_spare_servers = int(profile.get("pm_max_spare_servers", 8) or 8)
    pm_max_requests = int(profile.get("pm_max_requests", 1000) or 1000)
    req_timeout = str(profile.get("request_terminate_timeout", "120s")).strip() or "120s"
    listen_backlog = int(profile.get("listen_backlog", 4096) or 4096)

    lines: list[str] = [
        "; managed by mcd service profile (php-fpm hardware tuning)",
        f"; pool={pool}",
        f"pm = {pm}",
        f"pm.max_children = {pm_max_children}",
        f"pm.max_requests = {pm_max_requests}",
        f"request_terminate_timeout = {req_timeout}",
        f"listen.backlog = {listen_backlog}",
    ]
    if pm == "dynamic":
        lines.extend(
            [
                f"pm.start_servers = {pm_start_servers}",
                f"pm.min_spare_servers = {pm_min_spare_servers}",
                f"pm.max_spare_servers = {pm_max_spare_servers}",
            ]
        )
    return "\n".join(lines) + "\n"


def _build_opcache_override(profile: dict[str, Any]) -> str:
    opcache_mem = int(profile.get("opcache_memory_mb", 128) or 128)
    php_profile = profile.get("php")
    if not isinstance(php_profile, dict):
        php_profile = {}
    realpath_cache_size_kb = int(php_profile.get("realpath_cache_size_kb", 16384) or 16384)
    realpath_cache_ttl_sec = int(php_profile.get("realpath_cache_ttl_sec", 600) or 600)
    return (
        "; managed by mcd service profile (php-fpm hardware tuning)\n"
        "opcache.enable=1\n"
        f"opcache.memory_consumption={opcache_mem}\n"
        "opcache.interned_strings_buffer=16\n"
        "opcache.max_accelerated_files=20000\n"
        "opcache.revalidate_freq=2\n"
        "opcache.validate_timestamps=1\n"
        f"realpath_cache_size={realpath_cache_size_kb}K\n"
        f"realpath_cache_ttl={realpath_cache_ttl_sec}\n"
    )


def _build_redis_sessions_override(profile: dict[str, Any]) -> str:
    save_handler = str(profile.get("redis_session_save_handler", "redis")).strip() or "redis"
    save_path = str(profile.get("redis_session_save_path", "tcp://127.0.0.1:6379?database=10")).strip() or "tcp://127.0.0.1:6379?database=10"
    save_path = save_path.replace('"', '\\"')
    lock_enabled = int(profile.get("redis_session_locking_enabled", 1) or 1)
    lock_retries = int(profile.get("redis_session_lock_retries", -1) or -1)
    lock_wait = int(profile.get("redis_session_lock_wait_time", 10000) or 10000)
    return (
        "; managed by mcd service profile (redis sessions)\n"
        f"session.save_handler = {save_handler}\n"
        f"session.save_path = \"{save_path}\"\n\n"
        f"redis.session.locking_enabled = {lock_enabled}\n"
        f"redis.session.lock_retries = {lock_retries}\n"
        f"redis.session.lock_wait_time = {lock_wait}\n"
    )


def _build_sysctl_override(profile: dict[str, Any]) -> str:
    somaxconn = int(profile.get("sysctl_somaxconn", profile.get("listen_backlog", 4096)) or 4096)
    return (
        "# managed by mcd service profile (php-fpm hardware tuning)\n"
        f"net.core.somaxconn={somaxconn}\n"
    )


def _detect_mysql_service_name() -> str:
    candidates = ["mysql", "mariadb", "mysqld"]
    for svc in candidates:
        if _service_exists(svc):
            return svc
    return "mysql"


def _detect_mysql_engine() -> str:
    for bin_name in ("mysqld", "mariadbd"):
        path = shutil.which(bin_name)
        if not path:
            continue
        try:
            proc = subprocess.run([path, "--version"], capture_output=True, text=True)
            ver = f"{proc.stdout}\n{proc.stderr}".lower()
        except Exception:
            ver = ""
        if "mariadb" in ver:
            return "mariadb"
        if "percona" in ver:
            return "percona"
        if "mysql" in ver:
            return "mysql"
    return "mysql"


def _agent_cluster_mode(cfg: AgentConfig) -> bool:
    return bool(str(getattr(cfg, "cluster_id", "") or "").strip())


def _cluster_db_maintenance_guard(
    cfg: AgentConfig,
    component: str,
    *,
    allow_cluster_db_maintenance: bool = False,
) -> dict[str, Any] | None:
    """
    Cluster DB/schema changes must not be daemon side effects.

    Galera/PXC can turn even "online" ALTERs into cluster-wide TOI/NBO stalls.
    In cluster mode, DB-heavy service-profile components are therefore skipped
    unless an operator explicitly runs a maintenance command with the dedicated
    override flag.
    """
    comp = (component or "").strip().lower().replace("-", "_")
    if comp not in {"mysql", "mautic_db_indexes", "db_indexes"}:
        return None
    if not (_agent_cluster_mode(cfg) or _mysql_galera_config_detected()):
        return None
    if allow_cluster_db_maintenance:
        return None
    return {
        "status": "skipped",
        "reason": "cluster_db_maintenance_requires_explicit_operator_flag",
        "component": comp,
        "cluster_id": str(getattr(cfg, "cluster_id", "") or "").strip(),
        "manual_command": (
            "mcd-cli service-profile apply --component "
            f"{comp} --allow-cluster-db-maintenance"
        ),
    }


def _mysql_galera_config_detected() -> bool:
    paths = [
        Path("/etc/mysql/my.cnf"),
        Path("/etc/mysql/mysql.cnf"),
        Path("/etc/mysql/conf.d/galera.cnf"),
        Path("/etc/mysql/mysql.conf.d/galera.cnf"),
        Path("/etc/mysql/mysql.conf.d/zz-ananas-cluster-role.cnf"),
    ]
    try:
        for path in paths:
            if not path.exists() or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            if any(marker in text for marker in _MYSQL_GALERA_MARKERS):
                return True
    except Exception:
        return False
    return False


def _mysql_profile_allows_cluster_apply(profile: dict[str, Any]) -> bool:
    for key in ("cluster_safe", "galera_safe", "pxc_safe", "allow_cluster_apply"):
        if _as_bool(profile.get(key), False):
            return True
    scope = str(profile.get("scope", profile.get("profile_scope", "")) or "").strip().lower()
    return scope in {"cluster", "galera", "pxc", "percona-xtradb-cluster"}


def _sanitize_cluster_mysql_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """
    Keep Galera/PXC service-profile writes inside the proven safe envelope.

    Cluster nodes are not standalone MySQL boxes. A too-aggressive profile can
    amplify certification stalls across every node, so even a profile explicitly
    marked cluster-safe is bounded here before any file or dynamic SET GLOBAL is
    attempted.
    """
    out = dict(profile)
    caps = {
        "innodb_buffer_pool_size_mb": 98_304,
        "innodb_redo_log_capacity_mb": 4_096,
        "innodb_io_capacity": 12_000,
        "innodb_io_capacity_max": 24_000,
        "max_connections": 600,
        "thread_cache_size": 128,
        "table_open_cache": 8_000,
        "table_definition_cache": 4_000,
        "open_files_limit": 65_535,
        "tmp_table_size_mb": 256,
        "max_heap_table_size_mb": 256,
        "max_allowed_packet_mb": 64,
    }
    for key, cap in caps.items():
        raw = out.get(key)
        if raw is None:
            continue
        try:
            val = int(raw)
        except Exception:
            continue
        if val > cap:
            out[key] = cap
    out["cluster_safe"] = True
    out["scope"] = str(out.get("scope") or "pxc").strip() or "pxc"
    return out


def _detect_mysql_dropin(profile: dict[str, Any]) -> Path:
    raw = str(profile.get("dropin_filename", "99-mcd-hw.cnf")).strip() or "99-mcd-hw.cnf"
    p = Path(raw)
    if p.is_absolute():
        return p
    candidates = [
        Path("/etc/mysql/mysql.conf.d"),
        Path("/etc/mysql/mariadb.conf.d"),
        Path("/etc/mysql/conf.d"),
        Path("/etc/my.cnf.d"),
    ]
    for d in candidates:
        if d.exists() and d.is_dir():
            return d / raw
    return Path("/etc/mysql/conf.d") / raw


def _mysql_include_root_content() -> str:
    return (
        "# Managed by MCD service profile (mysql include root)\n"
        "# Keep server tuning in drop-in files so profile precedence is deterministic.\n\n"
        "!includedir /etc/mysql/conf.d/\n"
        "!includedir /etc/mysql/mysql.conf.d/\n"
    )


def _cleanup_legacy_mysql_top_level_configs() -> dict[Path, str | None]:
    """
    Older MCD ops tuning wrote a full [mysqld] section directly into top-level
    files after includedir statements. That makes profile drop-ins ineffective
    because later top-level values override them. Only rewrite files that are
    explicitly marked as old MCD-managed tuning.
    """
    before: dict[Path, str | None] = {}
    replacement = _mysql_include_root_content()
    for path in _MYSQL_TOP_LEVEL_CONFIGS:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        first_line = text.splitlines()[0].strip() if text.splitlines() else ""
        if not first_line.startswith("# Managed by MCD ops tuning"):
            continue
        before[path] = text
        _write_file(path, replacement)
    return before


def _cleanup_mysql_top_level_profile_overrides() -> dict[Path, str | None]:
    """
    Disable only active top-level MySQL settings that MCD now owns through its
    drop-in. Some old hosts have hand-copied /etc/mysql/my.cnf after includedir
    statements, so those values win over 99-mcd-hw.cnf after restart.

    We deliberately leave unrelated local settings (timeouts, event scheduler,
    flush method, etc.) intact and keep the disabled lines in-place as comments.
    """
    before: dict[Path, str | None] = {}
    section = re.compile(r"^\s*\[(?P<section>[^\]]+)\]\s*(?:[#;].*)?$")
    assignment = re.compile(r"^\s*(?P<key>[A-Za-z0-9_-]+)\s*=")
    for path in _MYSQL_TOP_LEVEL_CONFIGS:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        current_section = ""
        changed = False
        lines: list[str] = []
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            m_section = section.match(line)
            if m_section:
                current_section = m_section.group("section").strip().lower()
                lines.append(line)
                continue
            if (
                current_section == "mysqld"
                and stripped
                and not stripped.startswith(("#", ";"))
            ):
                m_assignment = assignment.match(line)
                if m_assignment:
                    key = m_assignment.group("key").strip().lower().replace("-", "_")
                    if key in _MYSQL_PROFILE_MANAGED_KEYS:
                        line = "# mcd-managed duplicate disabled: " + line
                        changed = True
            lines.append(line)
        if changed:
            before[path] = text
            _write_file(path, "".join(lines))
    return before


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _mb(size_mb: Any, default: int) -> str:
    try:
        v = int(size_mb)
    except Exception:
        v = int(default)
    if v < 1:
        v = int(default)
    return f"{v}M"


def _build_mysql_override(profile: dict[str, Any], *, engine: str) -> str:
    lines: list[str] = [
        "# managed by mcd service profile (mysql hardware tuning)",
        "[mysqld]",
        f"innodb_buffer_pool_size = {_mb(profile.get('innodb_buffer_pool_size_mb'), 2048)}",
    ]

    # This setting is supported by MySQL/Percona 8+, but is not portable across MariaDB versions.
    if engine in {"mysql", "percona"} and profile.get("innodb_redo_log_capacity_mb") is not None:
        lines.append(f"innodb_redo_log_capacity = {_mb(profile.get('innodb_redo_log_capacity_mb'), 1024)}")

    numeric_keys = [
        "innodb_io_capacity",
        "innodb_io_capacity_max",
        "max_connections",
        "thread_cache_size",
        "table_open_cache",
        "table_definition_cache",
        "open_files_limit",
    ]
    for key in numeric_keys:
        if profile.get(key) is None:
            continue
        try:
            val = int(profile.get(key))
        except Exception:
            continue
        if val > 0:
            lines.append(f"{key} = {val}")

    lines.append(f"tmp_table_size = {_mb(profile.get('tmp_table_size_mb'), 128)}")
    lines.append(f"max_heap_table_size = {_mb(profile.get('max_heap_table_size_mb'), 128)}")
    lines.append(f"max_allowed_packet = {_mb(profile.get('max_allowed_packet_mb'), 64)}")
    lines.append(f"skip_name_resolve = {1 if _as_bool(profile.get('skip_name_resolve'), True) else 0}")
    lines.append(
        f"log_queries_not_using_indexes = {'ON' if _as_bool(profile.get('log_queries_not_using_indexes'), False) else 'OFF'}"
    )
    return "\n".join(lines) + "\n"


def _mysql_client_cmd_candidates() -> list[list[str]]:
    client = shutil.which("mysql") or shutil.which("mariadb")
    if not client:
        return []
    candidates: list[list[str]] = []
    if os.geteuid() == 0:
        # Root installations commonly grant localhost/socket admin through
        # auth_socket. Use it for SET GLOBAL because debian.cnf often has
        # enough rights for status reads but not SYSTEM_VARIABLES_ADMIN/SUPER.
        candidates.append([client, "-u", "root"])
    debian_cnf = Path("/etc/mysql/debian.cnf")
    if debian_cnf.exists():
        candidates.append([client, "--defaults-extra-file=/etc/mysql/debian.cnf"])
    candidates.append([client])
    out: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for cmd in candidates:
        key = tuple(cmd)
        if key in seen:
            continue
        seen.add(key)
        out.append(cmd)
    return out


def _mysql_mb_bytes(profile: dict[str, Any], key: str, default_mb: int) -> int | None:
    raw = profile.get(key)
    if raw is None:
        raw = default_mb
    try:
        mb = int(raw)
    except Exception:
        return None
    if mb <= 0:
        return None
    return mb * 1024 * 1024


def _apply_mysql_dynamic_profile(profile: dict[str, Any], *, engine: str, timeout_sec: int = 20) -> dict[str, Any]:
    """
    Apply dynamic variables immediately. Static settings remain in the drop-in
    and take effect on the next controlled MySQL restart.
    """
    commands = _mysql_client_cmd_candidates()
    if not commands:
        return {"status": "skipped", "reason": "mysql_client_not_found"}

    statements: list[str] = []
    bp = _mysql_mb_bytes(profile, "innodb_buffer_pool_size_mb", 2048)
    if bp:
        statements.append(f"SET GLOBAL innodb_buffer_pool_size={bp}")
    redo = _mysql_mb_bytes(profile, "innodb_redo_log_capacity_mb", 1024)
    if redo and engine in {"mysql", "percona"}:
        statements.append(f"SET GLOBAL innodb_redo_log_capacity={redo}")
    for key in (
        "innodb_io_capacity",
        "innodb_io_capacity_max",
        "max_connections",
        "thread_cache_size",
        "table_open_cache",
        "table_definition_cache",
    ):
        raw = profile.get(key)
        if raw is None:
            continue
        try:
            val = int(raw)
        except Exception:
            continue
        if val > 0:
            statements.append(f"SET GLOBAL {key}={val}")
    for key, default_mb in (
        ("tmp_table_size_mb", 128),
        ("max_heap_table_size_mb", 128),
        ("max_allowed_packet_mb", 64),
    ):
        val = _mysql_mb_bytes(profile, key, default_mb)
        if val:
            statements.append(f"SET GLOBAL {key[:-3]}={val}")

    if not statements:
        return {"status": "noop", "applied": []}

    sql = ";\n".join(statements) + ";"
    errors: list[str] = []
    for cmd in commands:
        proc = subprocess.run(
            [*cmd, "--batch", "--skip-column-names", "-e", sql],
            capture_output=True,
            text=True,
            timeout=max(5, int(timeout_sec)),
        )
        if proc.returncode == 0:
            return {"status": "applied", "applied": statements, "client": " ".join(cmd)}
        err = (proc.stderr or proc.stdout or f"mysql_rc_{proc.returncode}").strip()
        errors.append(f"{' '.join(cmd)}: {err}")
    return {
        "status": "error",
        "reason": " | ".join(errors),
        "attempted": statements,
    }


def apply_php_fpm_profile(cfg: AgentConfig, profile: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeError("service-profile apply requires root")

    php_ver = _detect_php_version()
    php_bin = _find_php_fpm_bin(php_ver)
    service_name = _detect_php_fpm_service_name(php_ver)
    pool_name = str(profile.get("pool", "www")).strip() or "www"
    pool_override = Path(f"/etc/php/{php_ver}/fpm/pool.d/zz-mcd-hw.conf")
    opcache_fpm = Path(f"/etc/php/{php_ver}/fpm/conf.d/99-mcd-hw.ini")
    opcache_cli = Path(f"/etc/php/{php_ver}/cli/conf.d/99-mcd-hw.ini")
    redis_fpm = Path(f"/etc/php/{php_ver}/fpm/conf.d/90-redis-sessions.ini")
    redis_cli = Path(f"/etc/php/{php_ver}/cli/conf.d/90-redis-sessions.ini")

    pool_content = _build_pool_override(profile)
    opcache_content = _build_opcache_override(profile)
    redis_sessions_enabled = bool(profile.get("redis_sessions_enabled", True))
    redis_content = _build_redis_sessions_override(profile)
    sysctl_content = _build_sysctl_override(profile)

    before: dict[Path, str | None] = {
        pool_override: pool_override.read_text(encoding="utf-8") if pool_override.exists() else None,
        opcache_fpm: opcache_fpm.read_text(encoding="utf-8") if opcache_fpm.exists() else None,
        opcache_cli: opcache_cli.read_text(encoding="utf-8") if opcache_cli.exists() else None,
        redis_fpm: redis_fpm.read_text(encoding="utf-8") if redis_fpm.exists() else None,
        redis_cli: redis_cli.read_text(encoding="utf-8") if redis_cli.exists() else None,
        _SYSCTL_PATH: _SYSCTL_PATH.read_text(encoding="utf-8") if _SYSCTL_PATH.exists() else None,
    }

    if dry_run:
        files = [str(pool_override), str(opcache_fpm), str(opcache_cli), str(_SYSCTL_PATH)]
        if redis_sessions_enabled:
            files.insert(3, str(redis_fpm))
            files.insert(4, str(redis_cli))
        return {
            "status": "planned",
            "php_version": php_ver,
            "php_fpm_bin": php_bin,
            "service_name": service_name,
            "pool": pool_name,
            "files": files,
        }

    changed: list[str] = []
    try:
        for p in _remove_legacy_php_ini_baseline_files():
            changed.append(p)
        if _write_file(pool_override, pool_content):
            changed.append(str(pool_override))
        if _write_file(opcache_fpm, opcache_content):
            changed.append(str(opcache_fpm))
        if _write_file(opcache_cli, opcache_content):
            changed.append(str(opcache_cli))
        if redis_sessions_enabled:
            if _write_file(redis_fpm, redis_content):
                changed.append(str(redis_fpm))
            if _write_file(redis_cli, redis_content):
                changed.append(str(redis_cli))
        else:
            if redis_fpm.exists():
                redis_fpm.unlink()
                changed.append(str(redis_fpm))
            if redis_cli.exists():
                redis_cli.unlink()
                changed.append(str(redis_cli))
        if _write_file(_SYSCTL_PATH, sysctl_content):
            changed.append(str(_SYSCTL_PATH))

        if not changed:
            return {
                "status": "noop",
                "php_version": php_ver,
                "php_fpm_bin": php_bin,
                "service_name": service_name,
                "pool": pool_name,
                "changed_files": [],
            }

        proc_t = subprocess.run([php_bin, "-tt"], capture_output=True, text=True)
        if proc_t.returncode != 0:
            raise RuntimeError((proc_t.stderr or proc_t.stdout or "php-fpm -tt failed").strip())

        somaxconn = int(profile.get("sysctl_somaxconn", profile.get("listen_backlog", 4096)) or 4096)
        subprocess.run(["sysctl", "-w", f"net.core.somaxconn={somaxconn}"], capture_output=True, text=True)

        proc_reload = subprocess.run(["systemctl", "reload", service_name], capture_output=True, text=True)
        if proc_reload.returncode != 0:
            proc_restart = subprocess.run(["systemctl", "restart", service_name], capture_output=True, text=True)
            if proc_restart.returncode != 0:
                msg = (proc_restart.stderr or proc_restart.stdout or "php-fpm restart failed").strip()
                raise RuntimeError(msg)

        return {
            "status": "applied",
            "php_version": php_ver,
            "php_fpm_bin": php_bin,
            "service_name": service_name,
            "pool": pool_name,
            "changed_files": changed,
        }
    except Exception:
        for path, content in before.items():
            try:
                if content is None:
                    if path.exists():
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
            except Exception:
                pass
        raise


def apply_mysql_profile(cfg: AgentConfig, profile: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeError("service-profile apply requires root")

    service_name = _detect_mysql_service_name()
    engine = _detect_mysql_engine()
    cluster_mysql = _agent_cluster_mode(cfg) and _mysql_galera_config_detected()
    if cluster_mysql and not _mysql_profile_allows_cluster_apply(profile):
        return {
            "status": "skipped",
            "reason": "cluster_galera_mysql_profile_not_marked_safe",
            "engine": engine,
            "service_name": service_name,
            "cluster_id": str(getattr(cfg, "cluster_id", "") or "").strip(),
        }
    if cluster_mysql:
        profile = _sanitize_cluster_mysql_profile(profile)
    dropin = _detect_mysql_dropin(profile)
    content = _build_mysql_override(profile, engine=engine)
    before = dropin.read_text(encoding="utf-8") if dropin.exists() else None

    if dry_run:
        return {
            "status": "planned",
            "engine": engine,
            "service_name": service_name,
            "files": [str(dropin)],
        }

    changed: list[str] = []
    top_level_before: dict[Path, str | None] = {}
    try:
        if not cluster_mysql:
            top_level_before = _cleanup_legacy_mysql_top_level_configs()
            profile_override_before = _cleanup_mysql_top_level_profile_overrides()
            top_level_before.update(profile_override_before)
            changed.extend(str(p) for p in top_level_before)
        if _write_file(dropin, content):
            changed.append(str(dropin))

        dynamic = _apply_mysql_dynamic_profile(profile, engine=engine)

        dynamic_status = str(dynamic.get("status") or "").strip().lower()
        if not changed and dynamic_status in {"noop", "skipped"}:
            return {
                "status": "noop",
                "engine": engine,
                "service_name": service_name,
                "changed_files": [],
                "dynamic": dynamic,
            }
        if not changed and dynamic_status == "error":
            result_status = "deferred"
        elif dynamic_status == "error":
            result_status = "partial"
        else:
            result_status = "applied"

        proc_reload = subprocess.run(["systemctl", "reload", service_name], capture_output=True, text=True)
        if proc_reload.returncode != 0:
            # Avoid surprise restarts during profile drift correction. The
            # drop-in is durable and dynamic variables above cover the memory
            # pressure case immediately; static settings apply on a planned
            # restart/maintenance window.
            reload_note = (proc_reload.stderr or proc_reload.stdout or "mysql reload unsupported").strip()
        else:
            reload_note = "reloaded"
        return {
            "status": result_status,
            "engine": engine,
            "service_name": service_name,
            "changed_files": changed,
            "dynamic": dynamic,
            "reload": reload_note,
        }
    except Exception:
        try:
            if before is None:
                if dropin.exists():
                    dropin.unlink()
            else:
                dropin.parent.mkdir(parents=True, exist_ok=True)
                dropin.write_text(before, encoding="utf-8")
            for path, content_before in top_level_before.items():
                if content_before is None:
                    if path.exists():
                        path.unlink()
                else:
                    path.write_text(content_before, encoding="utf-8")
        except Exception:
            pass
        raise


def service_profiles_apply_once(
    cfg: AgentConfig,
    *,
    component: str = "php_fpm",
    dry_run: bool = False,
    force_apt_repo_rescan: bool = False,
    allow_cluster_db_maintenance: bool = False,
) -> dict[str, Any]:
    comp = (component or "php_fpm").strip().lower().replace("-", "_")
    if comp not in {"php_fpm", "mysql", "apt", "mautic_db_indexes", "db_indexes"}:
        return {"status": "skipped", "reason": f"unsupported component: {component}"}
    guard = _cluster_db_maintenance_guard(
        cfg,
        comp,
        allow_cluster_db_maintenance=allow_cluster_db_maintenance,
    )
    if guard is not None:
        return guard
    if comp in {"mautic_db_indexes", "db_indexes"}:
        applied = apply_mautic_db_indexes(cfg, dry_run=dry_run)
        status = str(applied.get("status") or "").strip().lower()
        if status in {"applied", "noop", "planned"}:
            return {"status": "ok", "fetch": {"status": "ok", "source": "builtin"}, "apply": applied}
        if status == "deferred":
            return {"status": "skipped", "reason": applied.get("reason", "deferred"), "apply": applied}
        return {"status": "error", "reason": applied.get("reason", status or "apply_failed"), "apply": applied}
    fetched = fetch_service_profile(cfg, comp)
    status = str(fetched.get("status", "")).strip().lower()
    if status != "ok":
        return {"status": "error", "reason": fetched.get("reason", status or "fetch_failed"), "fetch": fetched}
    profile = fetched.get("profile")
    if not isinstance(profile, dict):
        return {"status": "error", "reason": "invalid profile payload", "fetch": fetched}
    if comp == "php_fpm":
        applied = apply_php_fpm_profile(cfg, profile, dry_run=dry_run)
    elif comp == "mysql":
        applied = apply_mysql_profile(cfg, profile, dry_run=dry_run)
    else:
        if os.geteuid() != 0:
            raise RuntimeError("service-profile apply requires root")
        applied = apply_apt_profile(
            profile,
            dry_run=dry_run,
            cfg=cfg,
            force_repo_rescan=bool(force_apt_repo_rescan),
        )
    return {"status": "ok", "fetch": fetched, "apply": applied}
