from __future__ import annotations

from mcd_agent.mcc_transport import mcc_request

from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
from typing import Any
from urllib import request
from uuid import uuid4

from mcd_agent.archive_utils import safe_extract_tar
from mcd_agent.config import AgentConfig
from mcd_agent.nginx_external import (
    cleanup_managed_vhost as _external_cleanup_managed_vhost,
    write_managed_nginx_vhost as _external_write_managed_nginx_vhost,
)
from mcd_agent.source_operation import stable_source_operation
from mcd_agent.upgrade_os_external import run_upgrade_os_operation


@dataclass
class ImageInstallPlan:
    image_ref: str
    domain: str
    short_name: str
    db_name: str
    db_user: str
    db_password: str
    webroot: Path
    php_version: str


def _run(args: list[str], *, timeout_sec: int = 300, input_text: str | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    return int(proc.returncode), ((proc.stdout or "") + (proc.stderr or "")).strip()


def _domain(raw: str) -> str:
    domain = str(raw or "").strip().lower().rstrip(".")
    if not re.match(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", domain):
        raise RuntimeError(f"invalid domain: {raw}")
    return domain


def _short(domain: str) -> str:
    value = re.sub(r"[^a-z0-9_]+", "_", domain.split(".", 1)[0]).strip("_")
    if not value:
        raise RuntimeError("empty short name")
    return value[:48].rstrip("_")


def _mysql_bin() -> str:
    for name in ("mariadb", "mysql"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("mysql/mariadb client is missing")


_MYSQL_ADMIN_BASE: list[str] | None = None


def _mysql_default_files() -> list[Path]:
    return [
        Path("/root/.my.cnf"),
        Path("/etc/mysql/debian.cnf"),
        Path("/etc/mysql/my.cnf"),
        Path("/etc/my.cnf"),
    ]


def _mysql_probe_ok(out: str) -> bool:
    lines = [line.strip() for line in str(out or "").splitlines() if line.strip()]
    return bool(lines) and lines[-1] == "1"


def _describe_mysql_base(base: list[str]) -> str:
    for arg in base[1:]:
        if arg.startswith("--defaults-extra-file="):
            return f"{Path(base[0]).name} defaults={arg.split('=', 1)[1]}"
    return Path(base[0]).name


def _mysql_admin_base() -> list[str]:
    global _MYSQL_ADMIN_BASE
    if _MYSQL_ADMIN_BASE is not None:
        return list(_MYSQL_ADMIN_BASE)

    mysql = _mysql_bin()
    candidates: list[list[str]] = [[mysql]]
    for path in _mysql_default_files():
        if path.exists():
            candidates.append([mysql, f"--defaults-extra-file={path}"])

    failures: list[str] = []
    for base in candidates:
        rc, out = _run([*base, "-N", "-B", "-e", "SELECT 1"], timeout_sec=15)
        if rc == 0 and _mysql_probe_ok(out):
            _MYSQL_ADMIN_BASE = list(base)
            return list(base)
        summary = " ".join(str(out or "").split())[:180]
        failures.append(f"{_describe_mysql_base(base)}: {summary or 'failed'}")

    detail = "; ".join(failures[-4:])
    raise RuntimeError("mysql admin connection failed" + (f": {detail}" if detail else ""))


def _mysql_exec(sql: str, *, timeout_sec: int = 120) -> str:
    rc, out = _run([*_mysql_admin_base(), "-N", "-B", "-e", sql], timeout_sec=timeout_sec)
    if rc != 0:
        raise RuntimeError(out or "mysql command failed")
    return out


def _db_exists(name: str) -> bool:
    safe = name.replace("'", "''")
    out = _mysql_exec(f"SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME='{safe}'")
    return bool(out.strip())


def _db_user_exists(name: str) -> bool:
    safe = name.replace("'", "''")
    out = _mysql_exec(
        "SELECT User FROM mysql.user "
        f"WHERE User='{safe}' AND Host='localhost' LIMIT 1"
    )
    return bool(out.strip())


def _quote_ident(value: str) -> str:
    return "`" + str(value).replace("`", "``") + "`"


def _quote_sql(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_plan(*, image_ref: str, domain: str, php_version: str) -> ImageInstallPlan:
    d = _domain(domain)
    short = _short(d)
    db_name = f"baza_{short}"
    return ImageInstallPlan(
        image_ref=str(image_ref or "").strip(),
        domain=d,
        short_name=short,
        db_name=db_name,
        db_user=f"korisnik_{short}",
        db_password=secrets.token_urlsafe(24),
        webroot=Path("/var/www") / short / "public_html",
        php_version=str(php_version or "").strip(),
    )


def _selected_php_bin(plan: ImageInstallPlan) -> str:
    return f"/usr/bin/php{plan.php_version}"


def _preflight(plan: ImageInstallPlan, cfg: AgentConfig | None = None) -> list[str]:
    problems: list[str] = []
    if os.geteuid() != 0:
        problems.append("must run as root")
    if not re.match(r"^[0-9]+\.[0-9]+$", plan.php_version):
        problems.append("invalid php version")
    elif not Path(_selected_php_bin(plan)).is_file():
        problems.append(f"missing selected PHP CLI: {_selected_php_bin(plan)}")
    for cmd in ("tar", "gzip", "nginx", "php", "sudo", "curl"):
        if not shutil.which(cmd):
            problems.append(f"missing command: {cmd}")
    php_sock = Path(f"/run/php/php{plan.php_version}-fpm.sock")
    if not php_sock.exists():
        problems.append(f"missing PHP-FPM socket: {php_sock}")
    if plan.webroot.exists():
        problems.append(f"webroot already exists: {plan.webroot}")
    parent = plan.webroot.parent
    if parent.exists():
        if not parent.is_dir():
            problems.append(f"instance parent is not a directory: {parent}")
        else:
            try:
                if any(parent.iterdir()):
                    problems.append(f"instance parent is not empty: {parent}")
            except OSError as exc:
                problems.append(f"instance parent cannot be inspected: {parent}: {exc}")
    if cfg is not None:
        try:
            run_upgrade_os_operation(
                cfg,
                component="certificate",
                operation="certificate_material",
                operation_input={
                    "operation_id": "",
                    "domain": plan.domain,
                    "action": "inspect",
                    "webroot": "",
                    "email": "",
                    "certificate_ref": "",
                    "dry_run": "false",
                },
            )
        except Exception as exc:
            problems.append(f"certificate preflight failed: {type(exc).__name__}")
    try:
        if _db_exists(plan.db_name):
            problems.append(f"database already exists: {plan.db_name}")
        if _db_user_exists(plan.db_user):
            problems.append(f"database user already exists: {plan.db_user}@localhost")
    except Exception as exc:
        problems.append(f"database preflight failed: {exc}")
    return problems


def _rollback_failed_install(
    cfg: AgentConfig,
    plan: ImageInstallPlan,
    *,
    operation_id: str,
    database_owned: bool,
    certificate_owned: bool,
    prepared_root: Path,
    tree_materialized: bool,
    vhost_ownership_receipt: dict[str, Any] | None,
    mail_requested: bool,
) -> list[str]:
    errors: list[str] = []
    if mail_requested:
        try:
            from mcd_agent.local_mail import disable_local_mail

            disable_local_mail(cfg, domain=plan.domain)
        except Exception as exc:
            errors.append(f"local mail: {exc}")

    if vhost_ownership_receipt is not None:
        try:
            _external_cleanup_managed_vhost(
                operation_id=operation_id,
                domain=plan.domain,
                ownership_receipt=vhost_ownership_receipt,
                cfg=cfg,
            )
        except Exception as exc:
            errors.append(f"nginx managed vhost: {type(exc).__name__}")

    if certificate_owned:
        try:
            run_upgrade_os_operation(
                cfg,
                component="certificate",
                operation="certificate_material",
                operation_input={
                    "operation_id": operation_id,
                    "domain": plan.domain,
                    "action": "remove_owned",
                    "webroot": str(plan.webroot),
                    "email": "",
                    "certificate_ref": "",
                    "dry_run": "false",
                },
            )
        except Exception as exc:
            errors.append(f"certificate: {type(exc).__name__}")

    if tree_materialized:
        try:
            run_upgrade_os_operation(
                cfg,
                component="filesystem",
                operation="upgrade_tree_transition",
                operation_input={
                    "operation_id": operation_id,
                    "instance_root": str(plan.webroot),
                    "prepared_root": str(prepared_root),
                    "backup_id": "",
                    "action": "rollback_materialized",
                    "dry_run": "false",
                },
            )
        except Exception as exc:
            errors.append(f"webroot: {type(exc).__name__}")

    if vhost_ownership_receipt is not None:
        try:
            for action in ("test", "reload"):
                run_upgrade_os_operation(
                    cfg,
                    component="nginx",
                    operation="nginx_service_transition",
                    operation_input={
                        "operation_id": operation_id,
                        "target_php_version": plan.php_version,
                        "action": action,
                        "dry_run": "false",
                    },
                )
        except Exception as exc:
            errors.append(f"nginx: {type(exc).__name__}")

    if database_owned:
        try:
            run_upgrade_os_operation(
                cfg,
                component="database",
                operation="database_prepare_import",
                operation_input={
                    "operation_id": operation_id,
                    "engine": "mariadb",
                    "database": plan.db_name,
                    "database_user": plan.db_user,
                    "action": "rollback_owned",
                    "host": "127.0.0.1",
                    "port": "3306",
                    "unix_socket": "",
                    "dump_ref": "",
                    "target_db_credentials_json": "{}",
                    "dry_run": "false",
                },
            )
        except Exception as exc:
            errors.append(f"database: {type(exc).__name__}")
    return errors


def _download(url: str, token: str, dst: Path) -> None:
    req = mcc_request(url, headers={"Authorization": f"Bearer {token}"})
    with request.urlopen(req, timeout=1800) as resp, dst.open("wb") as f:
        shutil.copyfileobj(resp, f)


def _apt_install_role(cfg: AgentConfig, package_role: str) -> dict[str, Any]:
    return run_upgrade_os_operation(
        cfg,
        component="apt",
        operation="apt_apply",
        operation_input={
            "package_role": package_role,
            "package_state": "present",
            "force_repo_rescan": "true",
            "dry_run": "false",
        },
    )


def _artifact_url(cfg: AgentConfig, image_ref: str, kind: str) -> str:
    base = str(cfg.mcc_url or "").rstrip("/")
    if not base:
        raise RuntimeError("mcc.url is not configured")
    return f"{base}/api/v1/agent/mautic-images/{image_ref}/artifact/{kind}"


def _patch_local_php(path: Path, plan: ImageInstallPlan) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    text = text.replace("/var/www/ss/public_html", str(plan.webroot))
    text = re.sub(r"default[0-9a-z-]*\.sales-snap\.(?:com|ru)", plan.domain, text, flags=re.IGNORECASE)

    def php_string(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace("'", "\\'")

    def set_param(src: str, key: str, value: str) -> str:
        pattern = rf"(['\"]{re.escape(key)}['\"]\s*=>\s*)['\"][^'\"]*['\"]"
        quoted = "'" + php_string(value) + "'"
        new, count = re.subn(pattern, lambda m: m.group(1) + quoted, src)
        if count:
            return new
        marker = ");"
        insert = f"    '{key}' => {quoted},\n"
        idx = new.rfind(marker)
        if idx >= 0:
            return new[:idx] + insert + new[idx:]
        return new + "\n" + insert

    for key, value in {
        "db_host": "localhost",
        "db_port": "3306",
        "db_name": plan.db_name,
        "db_user": plan.db_user,
        "db_password": plan.db_password,
        "site_url": "https://" + plan.domain,
    }.items():
        text = set_param(text, key, value)
    path.write_text(text, encoding="utf-8")


def _write_nginx_vhost(plan: ImageInstallPlan, cfg: AgentConfig, *,
                       operation_id: str) -> dict[str, Any]:
    return _external_write_managed_nginx_vhost(
        operation_id=operation_id,
        root=plan.webroot,
        domains=[plan.domain],
        php_version=plan.php_version,
        cfg=cfg,
    )


def _skip_image_archive_member(name: str) -> bool:
    normalized = str(name or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized == ".mcd" or normalized.startswith(".mcd/")


def _safe_extract(tar: tarfile.TarFile, dst: Path) -> None:
    safe_extract_tar(tar, dst, allow_links=True, skip_prefixes=(".mcd",))


def _warm_image_runtime(plan: ImageInstallPlan) -> dict[str, Any]:
    console = plan.webroot / "bin" / "console"
    if not console.is_file():
        console = plan.webroot / "app" / "console"
    if not console.is_file():
        raise RuntimeError("image runtime console is missing")
    rc, _out = _run(
        ["sudo", "-n", "-u", "www-data", "--", _selected_php_bin(plan), str(console),
         "cache:warmup", "--env=prod", "--no-debug", "--no-interaction"],
        timeout_sec=600,
    )
    if rc != 0:
        # Console output may contain imported database credentials; keep errors bounded.
        raise RuntimeError(f"image prod cache warmup failed rc={rc}")
    return {"command": "cache:warmup", "environment": "prod", "debug": False,
            "runtime_user": "www-data", "php_bin": _selected_php_bin(plan), "status": "ok"}


def _verify_image_https(plan: ImageInstallPlan) -> dict[str, Any]:
    """Probe this exact local vhost with real hostname/SNI and normal CA validation."""
    import hashlib

    with tempfile.TemporaryDirectory(prefix="mcd-image-https-") as td:
        body = Path(td) / "login.html"
        rc, status = _run(
            ["curl", "--silent", "--show-error", "--noproxy", "*",
             "--connect-timeout", "5", "--max-time", "30",
             "--resolve", f"{plan.domain}:443:127.0.0.1",
             "--output", str(body), "--write-out", "%{http_code}",
             f"https://{plan.domain}/s/login"], timeout_sec=40,
        )
        if rc != 0 or status.strip() != "200":
            raise RuntimeError(f"image HTTPS login verification failed rc={rc} http={status.strip()[:3]}")
        html = body.read_bytes() if body.is_file() else b""
        if not re.search(rb"type\s*=\s*[\"']password[\"']", html, re.IGNORECASE):
            raise RuntimeError("image HTTPS response is not a login form")
        return {"status": "ok", "url": f"https://{plan.domain}/s/login", "http_status": 200,
                "connect_address": "127.0.0.1", "tls_verified": True,
                "login_form": True, "body_sha256": hashlib.sha256(html).hexdigest()}


@stable_source_operation
def install_from_image(
    cfg: AgentConfig,
    *,
    image_ref: str,
    domain: str,
    php_version: str,
    yes: bool = False,
    run_certbot: bool = True,
    own_mail: bool = False,
    mail_profile_id: str | None = None,
) -> dict[str, Any]:
    plan = build_plan(image_ref=image_ref, domain=domain, php_version=php_version)
    operation_id = f"image-{uuid4().hex}"
    if not yes:
        raise RuntimeError("--yes is required")
    if not str(cfg.mcc_token or "").strip():
        raise RuntimeError("mcc.token is not configured")

    _apt_install_role(cfg, "image_install")
    print(f"Preflight: domain={plan.domain} webroot={plan.webroot} db={plan.db_name}")
    problems = _preflight(plan, cfg)
    if problems:
        for p in problems:
            print(f"preflight_error: {p}")
        raise RuntimeError("preflight failed")

    with tempfile.TemporaryDirectory(prefix="mcd-image-install-") as td:
        tmp = Path(td)
        files_tgz = tmp / "files.tar.gz"
        db_artifact = tmp / "db.artifact"
        print("Downloading image files")
        _download(_artifact_url(cfg, plan.image_ref, "files"), str(cfg.mcc_token), files_tgz)
        print("Downloading image database")
        _download(_artifact_url(cfg, plan.image_ref, "db"), str(cfg.mcc_token), db_artifact)

        database_owned = False
        certificate_owned = False
        tree_materialized = False
        vhost_ownership_receipt: dict[str, Any] | None = None
        prepared_root = tmp / "prepared-root"
        mail_requested = bool(mail_profile_id or own_mail)
        try:
            print("Creating database, user and importing database")
            run_upgrade_os_operation(
                cfg,
                component="database",
                operation="database_prepare_import",
                operation_input={
                    "operation_id": operation_id,
                    "engine": "mariadb",
                    "database": plan.db_name,
                    "database_user": plan.db_user,
                    "action": "import",
                    "host": "127.0.0.1",
                    "port": "3306",
                    "unix_socket": "",
                    "dump_ref": str(db_artifact),
                    "target_db_credentials_json": json.dumps(
                        {"username": plan.db_user, "password": plan.db_password},
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                    "dry_run": "false",
                },
            )
            database_owned = True

            print("Extracting files")
            prepared_root.mkdir(parents=True, exist_ok=False)
            with tarfile.open(files_tgz, "r:gz") as tf:
                _safe_extract(tf, prepared_root)

            local_php = prepared_root / "config" / "local.php"
            if not local_php.exists():
                local_php = prepared_root / "app" / "config" / "local.php"
            if not local_php.exists():
                raise RuntimeError("local.php not found after extraction")
            print("Patching Mautic local.php")
            _patch_local_php(local_php, plan)

            for rel in ("var/cache", "var/logs"):
                imported = prepared_root / rel
                if imported.is_symlink():
                    imported.unlink()
                elif imported.exists():
                    shutil.rmtree(imported)

            print("Materializing image runtime with external ownership policy")
            run_upgrade_os_operation(
                cfg,
                component="filesystem",
                operation="upgrade_tree_transition",
                operation_input={
                    "operation_id": operation_id,
                    "instance_root": str(plan.webroot),
                    "prepared_root": str(prepared_root),
                    "backup_id": "",
                    "action": "materialize_fresh",
                    "dry_run": "false",
                },
            )
            tree_materialized = True

            print("Warming prod runtime before publishing nginx vhost")
            warmup = _warm_image_runtime(plan)

            print("Writing nginx vhost")
            vhost_result = _write_nginx_vhost(plan, cfg, operation_id=operation_id)
            vhost_ownership_receipt = vhost_result["ownership_receipt"]
            for action in ("test", "reload"):
                run_upgrade_os_operation(
                    cfg,
                    component="nginx",
                    operation="nginx_service_transition",
                    operation_input={
                        "operation_id": operation_id,
                        "target_php_version": plan.php_version,
                        "action": action,
                        "dry_run": "false",
                    },
                )

            if run_certbot:
                print("Running certbot via nginx HTTP-01")
                run_upgrade_os_operation(
                    cfg,
                    component="certificate",
                    operation="certificate_material",
                    operation_input={
                        "operation_id": operation_id,
                        "domain": plan.domain,
                        "action": "issue_http01",
                        "webroot": str(plan.webroot),
                        "email": "",
                        "certificate_ref": "",
                        "dry_run": "false",
                    },
                )
                certificate_owned = True

            if mail_profile_id:
                print("Applying MCC mail profile")
                from mcd_agent.mail_config import apply_mail_profile

                apply_mail_profile(
                    cfg,
                    profile_id=str(mail_profile_id),
                    domain=plan.domain,
                    root=str(plan.webroot),
                    php_bin=_selected_php_bin(plan),
                    validate_runtime=False,
                )
            elif own_mail:
                print("Configuring per-instance own-host mail")
                from mcd_agent.local_mail import configure_local_mail

                configure_local_mail(
                    cfg,
                    domain=plan.domain,
                    root=str(plan.webroot),
                    php_bin=_selected_php_bin(plan),
                    validate_runtime=False,
                )
            # Mail application can clear cache after its configuration mutation.
            if mail_requested:
                warmup = _warm_image_runtime(plan)
            print("Verifying local HTTPS login with certificate validation")
            runtime_verification = _verify_image_https(plan)
        except Exception as exc:
            rollback_errors = _rollback_failed_install(
                cfg,
                plan,
                operation_id=operation_id,
                database_owned=database_owned,
                certificate_owned=certificate_owned,
                prepared_root=prepared_root,
                tree_materialized=tree_materialized,
                vhost_ownership_receipt=vhost_ownership_receipt,
                mail_requested=mail_requested,
            )
            if rollback_errors:
                raise RuntimeError(f"{exc}; rollback errors: {'; '.join(rollback_errors)}") from exc
            raise

    from mcd_agent.inventory import InstanceInventory

    inv = InstanceInventory(cfg.state_db_path)
    count = inv.rescan(cfg)
    print(f"Rescan complete: {count} instances")
    return {
        "status": "ok",
        "domain": plan.domain,
        "webroot": str(plan.webroot),
        "db_name": plan.db_name,
        "php_version": plan.php_version,
        "mail_mode": "own_host" if own_mail or mail_profile_id else "external",
        "mail_profile_id": str(mail_profile_id or ""),
        "instances": count,
        "runtime_warmup": warmup,
        "runtime_verification": runtime_verification,
    }
