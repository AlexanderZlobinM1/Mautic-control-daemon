from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
from typing import Any

from dataclasses import replace

from mcd_agent.backup import _run_mydumper, _run_myloader, _verify_dump_dir
from mcd_agent.config import AgentConfig
from mcd_agent.db import MauticDB
from mcd_agent.install_readiness import collect_mautic_install_readiness
from mcd_agent.inventory import InstanceInventory, ensure_seeded
from mcd_agent.localphp import parse_local_php
from mcd_agent.mautic_image_install import _mysql_admin_base, _mysql_exec, _quote_ident, _quote_sql
from mcd_agent.models import DBConfig
from mcd_agent.models import MauticInstall


_MIN_TARGET_HEADROOM_BYTES = 5 * 1024 * 1024 * 1024
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_DB_IDENT_RE = re.compile(r"^[A-Za-z0-9_$-]{1,64}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(args: list[str], *, timeout_sec: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout_sec, check=False)
    except Exception as exc:
        return 1, str(exc)
    return int(proc.returncode), ((proc.stdout or "") + (proc.stderr or "")).strip()


def _run_checked(args: list[str], *, timeout_sec: int = 300, input_text: str | None = None) -> str:
    kwargs: dict[str, Any] = {
        "input": input_text,
        "capture_output": True,
        "text": True,
        "check": False,
    }
    if timeout_sec > 0:
        kwargs["timeout"] = timeout_sec
    proc = subprocess.run(args, **kwargs)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        raise RuntimeError(f"command failed rc={proc.returncode}: {' '.join(args[:4])} :: {out[-2000:]}")
    return out


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _is_local_db_host(host: str | None) -> bool:
    h = str(host or "").strip().lower()
    return h in {"", "localhost", "127.0.0.1", "::1", "_"}


def _clean_domain(raw: str) -> str:
    domain = str(raw or "").strip().lower().rstrip(".")
    if not domain or not _DOMAIN_RE.match(domain):
        raise RuntimeError(f"invalid domain: {raw}")
    return domain


def _json_domains(raw: str | None) -> list[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except Exception:
        parsed = []
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(parsed, list):
        parsed = []
    for item in parsed:
        d = _clean_domain(str(item or ""))
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _local_php_path(root: Path) -> Path:
    for rel in ("config/local.php", "app/config/local.php", "docroot/config/local.php"):
        p = root / rel
        if p.exists():
            return p
    raise RuntimeError(f"local.php not found under {root}")


def _db_from_local_php(root: Path) -> DBConfig:
    data = parse_local_php(str(_local_php_path(root)))
    host = str(data.get("db_host") or "localhost").strip() or "localhost"
    name = str(data.get("db_name") or "").strip()
    user = str(data.get("db_user") or "").strip()
    password = str(data.get("db_password") or "")
    prefix = str(data.get("db_table_prefix") or "")
    port = _safe_int(data.get("db_port"), 3306) or 3306
    if not (name and user and password):
        raise RuntimeError("local.php DB credentials are incomplete")
    if not _is_local_db_host(host):
        raise RuntimeError(f"only local source/target DB is supported for migration, got db_host={host}")
    return DBConfig(host="localhost", port=port, name=name, user=user, password=password, table_prefix=prefix)


def _target_db_from_values(*, source_db: DBConfig, name: str | None, user: str | None, password: str | None) -> DBConfig:
    db_name = str(name or source_db.name or "").strip()
    db_user = str(user or db_name or source_db.user or "").strip()
    db_password = str(password or "").strip() or secrets.token_urlsafe(24)
    if not _DB_IDENT_RE.match(db_name):
        raise RuntimeError(f"invalid target database name: {db_name}")
    if not _DB_IDENT_RE.match(db_user):
        raise RuntimeError(f"invalid target database user: {db_user}")
    return DBConfig(
        host="localhost",
        port=int(source_db.port or 3306),
        name=db_name,
        user=db_user,
        password=db_password,
        table_prefix=source_db.table_prefix,
    )


def _target_db_from_direct_values(*, name: str | None, user: str | None, password: str | None) -> DBConfig:
    db_name = str(name or "").strip()
    db_user = str(user or db_name or "").strip()
    db_password = str(password or "").strip()
    if not _DB_IDENT_RE.match(db_name):
        raise RuntimeError(f"invalid target database name: {db_name}")
    if not _DB_IDENT_RE.match(db_user):
        raise RuntimeError(f"invalid target database user: {db_user}")
    if not db_password:
        raise RuntimeError("target database password is required")
    return DBConfig(
        host="localhost",
        port=3306,
        name=db_name,
        user=db_user,
        password=db_password,
        table_prefix="",
    )


def _patch_local_php_db(root: Path, db: DBConfig) -> None:
    path = _local_php_path(root)
    text = path.read_text(encoding="utf-8", errors="ignore")
    replacements = {
        "db_host": "localhost",
        "db_port": str(int(db.port or 3306)),
        "db_name": db.name,
        "db_user": db.user,
        "db_password": db.password,
    }
    for key, value in replacements.items():
        pattern = rf"(['\"]{re.escape(key)}['\"]\s*=>\s*)['\"][^'\"]*['\"]"
        new_text, count = re.subn(pattern, lambda m, v=value: m.group(1) + _quote_sql(v), text, count=1)
        if count <= 0:
            raise RuntimeError(f"local.php key not found: {key}")
        text = new_text
    path.write_text(text, encoding="utf-8")


def _quote_for_ssh(value: str) -> str:
    import shlex

    return shlex.quote(str(value))


def _ssh_base(*, source_ssh_user: str, source_address: str, source_ssh_port: int, source_ssh_key_file: str) -> list[str]:
    return [
        "ssh",
        "-i",
        source_ssh_key_file,
        "-p",
        str(int(source_ssh_port or 22)),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        f"{source_ssh_user}@{source_address}",
    ]


def _remote_mcd(
    *,
    source_ssh_user: str,
    source_address: str,
    source_ssh_port: int,
    source_ssh_key_file: str,
    args: list[str],
    timeout_sec: int = 120,
) -> str:
    inner = "mcd-cli " + " ".join(_quote_for_ssh(x) for x in args)
    remote = f"if command -v sudo >/dev/null 2>&1; then sudo -n {inner}; else {inner}; fi"
    return _run_checked(
        [
            *_ssh_base(
                source_ssh_user=source_ssh_user,
                source_address=source_address,
                source_ssh_port=source_ssh_port,
                source_ssh_key_file=source_ssh_key_file,
            ),
            "bash",
            "-lc",
            remote,
        ],
        timeout_sec=timeout_sec,
    )


def _rsync_from_source(
    *,
    source_ssh_user: str,
    source_address: str,
    source_ssh_port: int,
    source_ssh_key_file: str,
    source_path: str,
    target_path: Path,
    delete: bool,
    excludes: list[str] | None = None,
) -> None:
    target_path.mkdir(parents=True, exist_ok=True)
    ssh_cmd = (
        f"ssh -i {_quote_for_ssh(source_ssh_key_file)} -p {int(source_ssh_port or 22)} "
        "-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "
        "-o ServerAliveInterval=15 -o ServerAliveCountMax=2"
    )
    cmd = [
        "rsync",
        "-aH",
        "--numeric-ids",
        "--partial",
        "--inplace",
        "-e",
        ssh_cmd,
    ]
    if delete:
        cmd.append("--delete")
    for pattern in list(excludes or []):
        cmd.extend(["--exclude", pattern])
    cmd.extend([f"{source_ssh_user}@{source_address}:{source_path.rstrip('/')}/", str(target_path) + "/"])
    _run_checked(cmd, timeout_sec=0)


def _find_free_local_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _open_source_db_tunnel(
    *,
    source_ssh_user: str,
    source_address: str,
    source_ssh_port: int,
    source_ssh_key_file: str,
    source_db_port: int,
) -> tuple[subprocess.Popen[Any], int]:
    local_port = _find_free_local_port()
    ssh = _ssh_base(
        source_ssh_user=source_ssh_user,
        source_address=source_address,
        source_ssh_port=source_ssh_port,
        source_ssh_key_file=source_ssh_key_file,
    )
    proc = subprocess.Popen(
        [
            *ssh[:-1],
            "-N",
            "-L",
            f"127.0.0.1:{local_port}:127.0.0.1:{int(source_db_port or 3306)}",
            ssh[-1],
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 12
    while time.time() < deadline:
        if proc.poll() is not None:
            err = ""
            try:
                _out, err = proc.communicate(timeout=1)
            except Exception:
                pass
            raise RuntimeError(f"source DB SSH tunnel failed: {err.strip()}")
        rc, _out = _run(["bash", "-lc", f"</dev/tcp/127.0.0.1/{local_port}"], timeout_sec=2)
        if rc == 0:
            return proc, local_port
        time.sleep(0.25)
    proc.terminate()
    raise RuntimeError("source DB SSH tunnel did not become ready")


def _close_proc(proc: subprocess.Popen[Any] | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def _prepare_target_db(db: DBConfig) -> None:
    _mysql_exec(f"DROP DATABASE IF EXISTS {_quote_ident(db.name)}")
    _mysql_exec(f"CREATE DATABASE {_quote_ident(db.name)} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    for host in ("localhost", "127.0.0.1"):
        _mysql_exec(
            "CREATE USER IF NOT EXISTS "
            + _quote_sql(db.user)
            + f"@'{host}' IDENTIFIED BY "
            + _quote_sql(db.password)
        )
        _mysql_exec(
            "ALTER USER "
            + _quote_sql(db.user)
            + f"@'{host}' IDENTIFIED BY "
            + _quote_sql(db.password)
        )
        _mysql_exec(f"GRANT ALL PRIVILEGES ON {_quote_ident(db.name)}.* TO {_quote_sql(db.user)}@'{host}'")
    _mysql_exec("FLUSH PRIVILEGES")


def preflight_target_relay(*, target_root: str, target_db_name: str) -> dict[str, Any]:
    target = Path(target_root).resolve()
    if not str(target).startswith("/"):
        raise RuntimeError("target root must be absolute")
    problems: list[str] = []
    if target.exists() and any(target.iterdir()):
        problems.append(f"target root already exists and is not empty: {target}")
    if not _DB_IDENT_RE.match(str(target_db_name or "")):
        problems.append(f"invalid target database name: {target_db_name}")
    else:
        try:
            if _target_db_exists(str(target_db_name)):
                problems.append(f"target database already exists: {target_db_name}")
        except Exception as exc:
            problems.append(f"target database preflight failed: {exc}")
    for cmd in ("tar", "gzip", "nginx", "php"):
        if not shutil.which(cmd):
            problems.append(f"missing command: {cmd}")
    return {
        "schema": "mcd-instance-migration-target-preflight-v1",
        "ok": not problems,
        "target_root": str(target),
        "target_db_name": str(target_db_name or ""),
        "problems": problems,
    }


def _target_db_exists(db_name: str) -> bool:
    safe = str(db_name or "").replace("'", "''")
    out = _mysql_exec(f"SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME='{safe}'")
    return bool(str(out or "").strip())


def stream_source_files(config: AgentConfig, *, selector: str | None, output: Any) -> int:
    inv = InstanceInventory(config.state_db_path)
    ensure_seeded(inv, config)
    inst = _select_instance(inv.list_instances(), selector)
    root = Path(inst.root).resolve()
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"source root is not a directory: {root}")
    cmd = [
        "tar",
        "--exclude=./var/cache",
        "--exclude=./var/logs",
        "--exclude=./var/tmp",
        "--exclude=./var/spool",
        "--exclude=./app/cache",
        "--exclude=./app/logs",
        "--exclude=./.git",
        "-C",
        str(root),
        "-czf",
        "-",
        ".",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    try:
        shutil.copyfileobj(proc.stdout, output)
    finally:
        proc.stdout.close()
    err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr is not None else ""
    rc = int(proc.wait())
    if rc != 0:
        raise RuntimeError(f"source file stream failed rc={rc}: {err.strip()}")
    return rc


def receive_target_files(*, target_root: str, input_stream: Any, wipe_target: bool = False) -> dict[str, Any]:
    target = Path(target_root).resolve()
    if not str(target).startswith("/"):
        raise RuntimeError("target root must be absolute")
    if wipe_target and target.exists():
        shutil.rmtree(target)
    if target.exists() and any(target.iterdir()):
        raise RuntimeError(f"target root already exists and is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["tar", "-xzf", "-", "-C", str(target)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        shutil.copyfileobj(input_stream, proc.stdin)
        proc.stdin.close()
        proc.stdin = None
        out_b, err_b = proc.communicate()
    except Exception:
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.stdin = None
        proc.kill()
        out_b, err_b = proc.communicate()
        raise
    if proc.returncode != 0:
        out = ((out_b or b"") + (err_b or b"")).decode("utf-8", errors="replace").strip()
        raise RuntimeError("target file extraction failed: " + out)
    return {"schema": "mcd-instance-migration-target-files-v1", "ok": True, "target_root": str(target), "wipe_target": bool(wipe_target)}


def _letsencrypt_paths_for_domains(domains: list[str]) -> list[Path]:
    root = Path("/etc/letsencrypt")
    paths: list[Path] = []
    seen: set[str] = set()
    for domain in domains:
        for rel in (Path("live") / domain, Path("archive") / domain, Path("renewal") / f"{domain}.conf"):
            path = root / rel
            key = str(path)
            if key in seen or not path.exists():
                continue
            seen.add(key)
            paths.append(path)
    return paths


def stream_source_letsencrypt(*, domains_json: str, output: Any) -> int:
    domains = _json_domains(domains_json)
    with tarfile.open(fileobj=output, mode="w|gz", dereference=False) as tf:
        for path in _letsencrypt_paths_for_domains(domains):
            tf.add(path, arcname=str(path).lstrip("/"), recursive=True)
    return 0


def _safe_tar_member_target(root: Path, member_name: str) -> Path:
    if not member_name or member_name.startswith("/"):
        raise RuntimeError(f"unsafe tar member path: {member_name}")
    if any(part == ".." for part in Path(member_name).parts):
        raise RuntimeError(f"unsafe tar member path: {member_name}")
    target = (root / member_name).resolve()
    root_resolved = root.resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise RuntimeError(f"unsafe tar member path: {member_name}") from exc
    return target


def receive_target_letsencrypt(*, input_stream: Any) -> dict[str, Any]:
    root = Path("/")
    extracted = 0
    with tarfile.open(fileobj=input_stream, mode="r|gz") as tf:
        for member in tf:
            _safe_tar_member_target(root, member.name)
            tf.extract(member, path=root)
            extracted += 1
    return {"schema": "mcd-instance-migration-target-letsencrypt-v1", "ok": True, "extracted": extracted}


def stream_source_db(config: AgentConfig, *, selector: str | None, output: Any) -> int:
    inv = InstanceInventory(config.state_db_path)
    ensure_seeded(inv, config)
    inst = _select_instance(inv.list_instances(), selector)
    source_db = _db_from_local_php(Path(inst.root).resolve())
    with tempfile.TemporaryDirectory(prefix="mcd-migrate-source-db-") as td:
        defaults = Path(td) / "db.cnf"
        defaults.write_text(
            "[client]\n"
            f"user={source_db.user}\n"
            f"password={source_db.password}\n"
            f"host={source_db.host or 'localhost'}\n"
            f"port={int(source_db.port or 3306)}\n",
            encoding="utf-8",
        )
        defaults.chmod(0o600)
        proc = subprocess.Popen(
            [
                "mysqldump",
                f"--defaults-extra-file={defaults}",
                "--single-transaction",
                "--quick",
                "--routines",
                "--triggers",
                "--events",
                source_db.name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        try:
            with gzip.GzipFile(fileobj=output, mode="wb") as gz:
                shutil.copyfileobj(proc.stdout, gz)
        finally:
            proc.stdout.close()
        err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr is not None else ""
        rc = int(proc.wait())
        if rc != 0:
            raise RuntimeError(f"source database stream failed rc={rc}: {err.strip()}")
        return rc


def import_target_db_stream(*, target_db_name: str, target_db_user: str, target_db_password: str, input_stream: Any) -> dict[str, Any]:
    target_db = _target_db_from_direct_values(name=target_db_name, user=target_db_user, password=target_db_password)
    _prepare_target_db(target_db)
    proc = subprocess.Popen([*_mysql_admin_base(), target_db.name], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        with gzip.GzipFile(fileobj=input_stream, mode="rb") as gz:
            shutil.copyfileobj(gz, proc.stdin)
        proc.stdin.close()
        proc.stdin = None
        out_b, err_b = proc.communicate()
    except Exception:
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.stdin = None
        proc.kill()
        out_b, err_b = proc.communicate()
        out = ((out_b or b"") + (err_b or b"")).decode("utf-8", errors="replace").strip()
        raise RuntimeError("target database import failed" + (": " + out if out else ""))
    if proc.returncode != 0:
        out = ((out_b or b"") + (err_b or b"")).decode("utf-8", errors="replace").strip()
        raise RuntimeError("target database import failed: " + out)
    return {"schema": "mcd-instance-migration-target-db-v1", "ok": True, "target_db_name": target_db.name, "target_db_user": target_db.user}


def finalize_target_relay(
    config: AgentConfig,
    *,
    target_root: str,
    target_db_name: str,
    target_db_user: str,
    target_db_password: str,
    domains_json: str,
    php_version: str | None = None,
) -> dict[str, Any]:
    target = Path(target_root).resolve()
    domains = _json_domains(domains_json)
    target_db = _target_db_from_direct_values(name=target_db_name, user=target_db_user, password=target_db_password)
    print("progress: 88 target web config")
    _patch_local_php_db(target, target_db)
    site = _write_nginx_vhost(root=target, domains=domains, php_version=php_version or "")
    _run_checked(["chown", "-R", "www-data:www-data", str(target)], timeout_sec=1800)
    print("progress: 94 target healthcheck")
    health = _target_healthcheck(target)
    inv = InstanceInventory(config.state_db_path)
    instances = inv.rescan(config)
    return {
        "schema": "mcd-instance-migration-result-v1",
        "ok": True,
        "catchup_ok": True,
        "completed_at_utc": _utc_now(),
        "target_root": str(target),
        "domains": domains,
        "target_db_name": target_db.name,
        "target_db_user": target_db.user,
        "nginx_vhost": site,
        "healthcheck_tail": health,
        "instances": instances,
    }


def _dump_source_into_target(
    cfg: AgentConfig,
    *,
    local_source_db: DBConfig,
    target_db: DBConfig,
    label: str,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"mcd-migrate-{label}-") as td:
        dump_dir = Path(td) / "dump"
        dump_dir.mkdir(parents=True, exist_ok=True)
        extra_args = [str(x).strip() for x in list(getattr(cfg, "backup_mydumper_extra_args", []) or []) if str(x).strip()]
        if not any(x in {"--rows", "-r", "--chunk-filesize", "-F"} or x.startswith(("--rows=", "-r=", "--chunk-filesize=", "-F=")) for x in extra_args):
            extra_args.append("--rows=500000")
        dump_cfg = replace(
            cfg,
            backup_method="mydumper",
            backup_mydumper_threads=max(8, int(getattr(cfg, "backup_mydumper_threads", 6) or 6)),
            backup_myloader_threads=max(8, int(getattr(cfg, "backup_myloader_threads", 6) or 6)),
            backup_mydumper_extra_args=extra_args,
        )
        _run_mydumper(dump_cfg, local_source_db, dump_dir)
        ok, msg, bytes_written = _verify_dump_dir(dump_dir)
        if not ok:
            raise RuntimeError(f"{label} dump verification failed: {msg}")
        _prepare_target_db(target_db)
        _run_myloader(dump_cfg, target_db, dump_dir)
        return {"label": label, "dump_bytes": int(bytes_written)}


def _detect_php_version(raw: str | None = None) -> str:
    if raw and re.fullmatch(r"\d+\.\d+", str(raw).strip()):
        return str(raw).strip()
    sockets = sorted(Path("/run/php").glob("php*-fpm.sock")) if Path("/run/php").exists() else []
    for sock in sockets:
        m = re.search(r"php(\d+\.\d+)-fpm\.sock", sock.name)
        if m:
            return m.group(1)
    return ""


def _nginx_web_root(root: Path) -> Path:
    for rel in ("docroot", "public"):
        candidate = root / rel
        if (candidate / "index.php").exists():
            return candidate
    return root


def _write_nginx_vhost(*, root: Path, domains: list[str], php_version: str) -> str:
    if not domains:
        raise RuntimeError("at least one domain is required for nginx vhost")
    php = _detect_php_version(php_version)
    if not php:
        raise RuntimeError("target PHP-FPM version is not available")
    sock = Path(f"/run/php/php{php}-fpm.sock")
    if not sock.exists():
        raise RuntimeError(f"target PHP-FPM socket is missing: {sock}")
    primary = domains[0]
    web_root = _nginx_web_root(root)
    site = Path("/etc/nginx/sites-available") / f"{primary}.conf"
    enabled = Path("/etc/nginx/sites-enabled") / f"{primary}.conf"
    server_names = " ".join(domains)
    cert_live = Path("/etc/letsencrypt/live") / primary
    ssl_block = ""
    listen = "listen 80;\n    listen [::]:80;"
    if (cert_live / "fullchain.pem").exists() and (cert_live / "privkey.pem").exists():
        listen = "listen 80;\n    listen [::]:80;\n    listen 443 ssl http2;\n    listen [::]:443 ssl http2;"
        ssl_block = f"""
    ssl_certificate /etc/letsencrypt/live/{primary}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{primary}/privkey.pem;
"""
    content = f"""server {{
    {listen}
    server_name {server_names};
    root {web_root};
    index index.php index.html index.htm;
{ssl_block}
    client_max_body_size 128m;

    location / {{
        try_files $uri /index.php$is_args$args;
    }}

    location ~ \\.php$ {{
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:{sock};
        fastcgi_read_timeout 300;
    }}

    location ~* ^/(?:app|bin|config|vendor|var)/ {{
        deny all;
    }}
}}
"""
    site.write_text(content, encoding="utf-8")
    enabled.parent.mkdir(parents=True, exist_ok=True)
    if enabled.exists() or enabled.is_symlink():
        enabled.unlink()
    enabled.symlink_to(site)
    _run_checked(["nginx", "-t"], timeout_sec=30)
    _run_checked(["systemctl", "reload", "nginx"], timeout_sec=30)
    return str(site)


def _copy_letsencrypt(
    *,
    source_ssh_user: str,
    source_address: str,
    source_ssh_port: int,
    source_ssh_key_file: str,
) -> bool:
    rc, _out = _run(
        [
            *_ssh_base(
                source_ssh_user=source_ssh_user,
                source_address=source_address,
                source_ssh_port=source_ssh_port,
                source_ssh_key_file=source_ssh_key_file,
            ),
            "test",
            "-d",
            "/etc/letsencrypt",
        ],
        timeout_sec=20,
    )
    if rc != 0:
        return False
    Path("/etc/letsencrypt").mkdir(parents=True, exist_ok=True)
    _rsync_from_source(
        source_ssh_user=source_ssh_user,
        source_address=source_address,
        source_ssh_port=source_ssh_port,
        source_ssh_key_file=source_ssh_key_file,
        source_path="/etc/letsencrypt",
        target_path=Path("/etc/letsencrypt"),
        delete=False,
    )
    return True


def _console(root: Path) -> Path:
    p = root / "bin" / "console"
    if p.exists():
        return p
    raise RuntimeError(f"Mautic console not found: {p}")


def _target_healthcheck(root: Path) -> list[str]:
    lines: list[str] = []
    console = _console(root)
    for args in (
        ["sudo", "-u", "www-data", "php", str(console), "--version"],
        ["sudo", "-u", "www-data", "php", str(console), "cache:clear", "--env=prod"],
    ):
        out = _run_checked(args, timeout_sec=600)
        if out:
            lines.extend(out.splitlines()[:4])
    return lines[-8:]


def run_target_pull_migration(
    config: AgentConfig,
    *,
    source_address: str,
    source_ssh_user: str,
    source_ssh_port: int,
    source_ssh_key_file: str,
    source_root: str,
    target_root: str,
    domains_json: str,
    target_db_name: str | None = None,
    target_db_user: str | None = None,
    target_db_password: str | None = None,
    php_version: str | None = None,
) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeError("target migration runner must run as root")
    if not shutil.which("rsync"):
        raise RuntimeError("rsync is required on target host")
    if not Path(source_ssh_key_file).exists():
        raise RuntimeError("source SSH key file is missing on target")
    source_root_clean = str(source_root or "").strip()
    if not source_root_clean.startswith("/"):
        raise RuntimeError("source root must be absolute")
    target = Path(target_root).resolve()
    if not str(target).startswith("/"):
        raise RuntimeError("target root must be absolute")
    if target.exists() and any(target.iterdir()):
        raise RuntimeError(f"target root already exists and is not empty: {target}")
    domains = _json_domains(domains_json)

    started = _utc_now()
    result: dict[str, Any] = {
        "schema": "mcd-instance-migration-result-v1",
        "ok": False,
        "catchup_ok": False,
        "started_at_utc": started,
        "target_root": str(target),
        "domains": domains,
        "target_db_name": str(target_db_name or ""),
        "target_db_user": str(target_db_user or ""),
        "steps": [],
    }

    exclusions = [
        "/var/cache/***",
        "/var/logs/***",
        "/var/tmp/***",
        "/app/cache/***",
        "/app/logs/***",
    ]
    source_maintenance_on = False
    tunnel: subprocess.Popen[Any] | None = None
    try:
        print("progress: 5 preflight target")
        _run_checked([*_mysql_admin_base(), "-N", "-B", "-e", "SELECT 1"], timeout_sec=30)

        print("progress: 10 initial file sync")
        _rsync_from_source(
            source_ssh_user=source_ssh_user,
            source_address=source_address,
            source_ssh_port=source_ssh_port,
            source_ssh_key_file=source_ssh_key_file,
            source_path=source_root_clean,
            target_path=target,
            delete=True,
            excludes=exclusions,
        )
        copied_source_db = _db_from_local_php(target)
        target_db = _target_db_from_values(
            source_db=copied_source_db,
            name=target_db_name,
            user=target_db_user,
            password=target_db_password,
        )
        if _target_db_exists(target_db.name):
            raise RuntimeError(f"target database already exists: {target_db.name}")
        result["target_db_name"] = target_db.name
        result["target_db_user"] = target_db.user
        source_db_port = int(copied_source_db.port or 3306)

        print("progress: 20 source DB tunnel")
        tunnel, local_port = _open_source_db_tunnel(
            source_ssh_user=source_ssh_user,
            source_address=source_address,
            source_ssh_port=source_ssh_port,
            source_ssh_key_file=source_ssh_key_file,
            source_db_port=source_db_port,
        )
        source_db = DBConfig(
            host="127.0.0.1",
            port=local_port,
            name=copied_source_db.name,
            user=copied_source_db.user,
            password=copied_source_db.password,
            table_prefix=copied_source_db.table_prefix,
        )

        print("progress: 30 initial database import")
        result["steps"].append(_dump_source_into_target(config, local_source_db=source_db, target_db=target_db, label="initial"))

        print("progress: 60 source maintenance on")
        _remote_mcd(
            source_ssh_user=source_ssh_user,
            source_address=source_address,
            source_ssh_port=source_ssh_port,
            source_ssh_key_file=source_ssh_key_file,
            args=["maintenance", "on", "--stop-cron", "--kill-orphans", "--grace-sec", "30", "--json"],
            timeout_sec=180,
        )
        source_maintenance_on = True

        print("progress: 68 final file sync")
        _rsync_from_source(
            source_ssh_user=source_ssh_user,
            source_address=source_address,
            source_ssh_port=source_ssh_port,
            source_ssh_key_file=source_ssh_key_file,
            source_path=source_root_clean,
            target_path=target,
            delete=True,
            excludes=exclusions,
        )
        _patch_local_php_db(target, target_db)

        print("progress: 76 final database catch-up")
        result["steps"].append(_dump_source_into_target(config, local_source_db=source_db, target_db=target_db, label="final"))

        print("progress: 88 target web config")
        copied_certs = _copy_letsencrypt(
            source_ssh_user=source_ssh_user,
            source_address=source_address,
            source_ssh_port=source_ssh_port,
            source_ssh_key_file=source_ssh_key_file,
        )
        site = _write_nginx_vhost(root=target, domains=domains, php_version=php_version or "")
        _run_checked(["chown", "-R", "www-data:www-data", str(target)], timeout_sec=1800)

        print("progress: 94 target healthcheck")
        health = _target_healthcheck(target)
        inv = InstanceInventory(config.state_db_path)
        instances = inv.rescan(config)

        result.update(
            {
                "ok": True,
                "catchup_ok": True,
                "completed_at_utc": _utc_now(),
                "source_maintenance_active": source_maintenance_on,
                "nginx_vhost": site,
                "letsencrypt_copied": copied_certs,
                "healthcheck_tail": health,
                "instances": instances,
            }
        )
        print("progress: 100 migration complete")
        return result
    except Exception:
        if source_maintenance_on:
            try:
                _remote_mcd(
                    source_ssh_user=source_ssh_user,
                    source_address=source_address,
                    source_ssh_port=source_ssh_port,
                    source_ssh_key_file=source_ssh_key_file,
                    args=["maintenance", "off", "--json"],
                    timeout_sec=120,
                )
                result["source_maintenance_restored"] = True
            except Exception as restore_exc:
                result["source_maintenance_restore_error"] = str(restore_exc)
        raise
    finally:
        _close_proc(tunnel)


def _du_bytes(path: Path) -> int:
    rc, out = _run(["du", "-sb", str(path)], timeout_sec=300)
    if rc == 0:
        first = (out.splitlines() or [""])[0].split()
        if first:
            try:
                return int(first[0])
            except Exception:
                pass
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() or child.is_symlink():
                total += int(child.lstat().st_size)
        except Exception:
            continue
    return total


def _df_payload(path: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(path)
    out: dict[str, Any] = {
        "path": str(path),
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "free_bytes": int(usage.free),
        "used_pct": round((float(usage.used) / float(usage.total)) * 100.0, 2) if usage.total else 0.0,
        "mount": "",
        "device": "",
    }
    rc, raw = _run(["df", "-P", str(path)], timeout_sec=10)
    if rc == 0:
        lines = [x for x in raw.splitlines() if x.strip()]
        if len(lines) >= 2:
            cols = lines[-1].split()
            if len(cols) >= 6:
                out["device"] = cols[0]
                out["mount"] = cols[5]
    return out


def _version_tuple(raw: str) -> list[int]:
    nums = [int(x) for x in re.findall(r"\d+", str(raw or ""))[:3]]
    while len(nums) < 3:
        nums.append(0)
    return nums


def _database_probe(inst: MauticInstall) -> dict[str, Any]:
    if inst.db is None:
        return {"ok": False, "error": "instance DB is not configured"}
    db = MauticDB(inst.db)
    local = _is_local_db_host(inst.db.host)
    out: dict[str, Any] = {
        "ok": False,
        "host": inst.db.host,
        "port": int(inst.db.port or 3306),
        "name": inst.db.name,
        "table_prefix": inst.db.table_prefix,
        "local": local,
        "engine": "",
        "version": "",
        "version_comment": "",
        "version_tuple": [0, 0, 0],
        "size_bytes": 0,
        "log_bin": None,
        "binlog_format": "",
        "server_id": 0,
        "read_only": None,
        "super_read_only": None,
        "final_sync_supported": False,
        "catchup_supported": False,
        "catchup_blockers": [],
    }
    blockers: list[str] = []
    if not local:
        blockers.append("db_not_local")
    try:
        rows = db.fetch_rows(
            """
            SELECT
              VERSION() AS version,
              @@version_comment AS version_comment,
              @@global.log_bin AS log_bin,
              @@global.binlog_format AS binlog_format,
              @@global.server_id AS server_id,
              @@global.read_only AS read_only
            """,
            limit=1,
        )
        row = rows[0] if rows else {}
        version = str(row.get("version") or "")
        comment = str(row.get("version_comment") or "")
        engine = "mariadb" if "mariadb" in (version + " " + comment).lower() else "mysql"
        out.update(
            {
                "ok": True,
                "engine": engine,
                "version": version,
                "version_comment": comment,
                "version_tuple": _version_tuple(version),
                "log_bin": bool(_safe_int(row.get("log_bin"), 0)),
                "binlog_format": str(row.get("binlog_format") or ""),
                "server_id": _safe_int(row.get("server_id"), 0),
                "read_only": bool(_safe_int(row.get("read_only"), 0)),
            }
        )
    except Exception as exc:
        out["error"] = str(exc)
        blockers.append("db_probe_failed")

    try:
        rows = db.fetch_rows(
            """
            SELECT COALESCE(SUM(DATA_LENGTH + INDEX_LENGTH), 0) AS size_bytes
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
            """,
            limit=1,
        )
        out["size_bytes"] = _safe_int((rows[0] if rows else {}).get("size_bytes"), 0)
    except Exception as exc:
        out["size_error"] = str(exc)
        blockers.append("db_size_probe_failed")

    try:
        rows = db.fetch_rows("SELECT @@global.super_read_only AS super_read_only", limit=1)
        if rows:
            out["super_read_only"] = bool(_safe_int(rows[0].get("super_read_only"), 0))
    except Exception:
        out["super_read_only"] = None

    if out.get("ok"):
        if not bool(out.get("log_bin")):
            blockers.append("source_binlog_disabled")
        if _safe_int(out.get("server_id"), 0) <= 0:
            blockers.append("source_server_id_missing")
    out["final_sync_supported"] = bool(out.get("ok")) and local
    out["catchup_blockers"] = sorted(set(blockers))
    out["catchup_supported"] = not out["catchup_blockers"]
    return out


def _select_instance(instances: list[MauticInstall], selector: str | None) -> MauticInstall:
    token = str(selector or "").strip()
    if not token:
        if len(instances) == 1:
            return instances[0]
        raise RuntimeError("instance root/name is required when host has multiple instances")
    token_l = token.lower()
    for inst in instances:
        candidates = {
            str(inst.root or "").strip(),
            str(inst.instance_uid or "").strip(),
            str(inst.name or "").strip(),
            str(inst.primary_domain or "").strip(),
            *[str(x or "").strip() for x in list(inst.domains or [])],
        }
        if token in candidates or token_l in {x.lower() for x in candidates if x}:
            return inst
    raise RuntimeError(f"instance not found: {token}")


def collect_source_probe(config: AgentConfig, selector: str | None = None) -> dict[str, Any]:
    inv = InstanceInventory(config.state_db_path)
    ensure_seeded(inv, config)
    instances = inv.list_instances()
    inst = _select_instance(instances, selector)
    root = Path(inst.root).resolve()
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"instance root is not a directory: {root}")

    files_bytes = _du_bytes(root)
    db_payload = _database_probe(inst)
    db_bytes = _safe_int(db_payload.get("size_bytes"), 0)
    payload: dict[str, Any] = {
        "schema": "mcd-instance-migration-source-probe-v1",
        "ok": True,
        "checked_at_utc": _utc_now(),
        "instance": {
            "instance_uid": inst.instance_uid,
            "name": inst.name,
            "root": str(root),
            "primary_domain": inst.primary_domain or "",
            "domains": list(inst.domains or []),
            "mautic_major": inst.mautic_major,
            "source": inst.source,
        },
        "files": {
            "bytes": files_bytes,
            "filesystem": _df_payload(root),
        },
        "database": db_payload,
        "requirements": {
            "estimated_source_bytes": int(files_bytes + db_bytes),
            "target_min_free_bytes": int(max(_MIN_TARGET_HEADROOM_BYTES, (files_bytes + db_bytes) * 1.35)),
            "same_database_engine_required": True,
            "same_major_database_version_recommended": True,
            "dns_cutover_requires_catchup_ok": True,
        },
        "readiness": collect_mautic_install_readiness(),
    }
    return payload


def format_source_probe_text(payload: dict[str, Any]) -> str:
    inst = payload.get("instance", {}) if isinstance(payload, dict) else {}
    files = payload.get("files", {}) if isinstance(payload, dict) else {}
    db = payload.get("database", {}) if isinstance(payload, dict) else {}
    req = payload.get("requirements", {}) if isinstance(payload, dict) else {}
    lines = [
        f"instance={inst.get('name', '-')}",
        f"root={inst.get('root', '-')}",
        f"files_bytes={files.get('bytes', 0)}",
        f"db_engine={db.get('engine', '-')}",
        f"db_version={db.get('version', '-')}",
        f"db_bytes={db.get('size_bytes', 0)}",
        f"catchup_supported={str(bool(db.get('catchup_supported'))).lower()}",
        f"catchup_blockers={','.join(list(db.get('catchup_blockers') or []))}",
        f"target_min_free_bytes={req.get('target_min_free_bytes', 0)}",
    ]
    return "\n".join(lines)


def format_source_probe_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, indent=2)
