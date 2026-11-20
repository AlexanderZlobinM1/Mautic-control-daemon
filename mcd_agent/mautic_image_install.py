from __future__ import annotations

from mcd_agent.mcc_transport import mcc_request
from mcd_agent.mysql_dump_compat import iter_mysql_import_sql

from dataclasses import dataclass
from pathlib import Path
import gzip
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from typing import Any
from urllib import request
from urllib.parse import urlencode

from mcd_agent.config import AgentConfig
from mcd_agent.form_embed import render_form_embed_location
from mcd_agent.instance_identity import (
    identity_readback,
    instance_path_names,
    validate_instance_code,
    validate_mcc_instance_uid,
    write_instance_identity,
)
from mcd_agent.nginx_templates import render_nginx_template
from mcd_agent.source_operation import stable_source_operation


_LETSENCRYPT_LIVE = Path("/etc/letsencrypt/live")
_LETSENCRYPT_RENEWAL = Path("/etc/letsencrypt/renewal")
_LETSENCRYPT_MCD = Path("/etc/letsencrypt/mcd")
_CERTBOT_LOCK_RETRY_SEC = 5
_CERTBOT_LOCK_MAX_WAIT_SEC = 180
_IMAGE_HTTPS_ATTEMPTS = 8
_IMAGE_HTTPS_RETRY_SEC = 1


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
    instance_uid: str | None = None
    instance_code: str | None = None


class ImageHTTPSVerificationError(RuntimeError):
    """A safe, structured failure from the final image HTTPS acceptance gate."""

    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


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


def _safe_runtime_detail(value: object, *, limit: int = 500) -> str:
    """Keep command diagnostics useful without allowing credentials into receipts."""
    text = " ".join(str(value or "").split())
    text = re.sub(
        r"(?i)\b(authorization|bearer|token|password|api[_-]?key)\b\s*[:=]\s*[^\s]+",
        r"\1=<redacted>",
        text,
    )
    return text[:limit]


def _utc_iso(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat()


def _certificate_datetime(cert: Any, *, utc_name: str, legacy_name: str) -> datetime:
    value = getattr(cert, utc_name, None)
    if value is None:
        value = getattr(cert, legacy_name)
    return value


def _certificate_matches_domain(domain: str, names: list[str]) -> bool:
    wanted = str(domain or "").strip().lower().rstrip(".")
    for raw in names:
        name = str(raw or "").strip().lower().rstrip(".")
        if name == wanted:
            return True
        if name.startswith("*.") and wanted.endswith(name[1:]) and wanted.count(".") == name.count("."):
            return True
    return False


def _certificate_identity_from_object(cert: Any, *, domain: str, source: str, path: str = "") -> dict[str, Any]:
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = [str(value) for value in san.value.get_values_for_type(x509.DNSName)]
    except Exception:
        dns_names = []
    common_names = [
        str(attr.value)
        for attr in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if str(attr.value).strip()
    ]
    names = dns_names or common_names
    not_before = _certificate_datetime(cert, utc_name="not_valid_before_utc", legacy_name="not_valid_before")
    not_after = _certificate_datetime(cert, utc_name="not_valid_after_utc", legacy_name="not_valid_after")
    return {
        "status": "ok",
        "source": source,
        "path": path,
        "server_name": str(domain),
        "dns_names": dns_names[:64],
        "common_names": common_names[:16],
        "hostname_matches": _certificate_matches_domain(domain, names),
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "serial": format(int(cert.serial_number), "x"),
        "not_before": _utc_iso(not_before),
        "not_after": _utc_iso(not_after),
    }


def _load_pem_certificate_identity(cert_path: Path, *, domain: str) -> dict[str, Any]:
    from cryptography import x509

    if not cert_path.is_file():
        raise RuntimeError(f"issued certificate is missing: {cert_path}")
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except Exception as exc:
        raise RuntimeError(f"issued certificate cannot be parsed: {cert_path}") from exc
    evidence = _certificate_identity_from_object(cert, domain=domain, source="issued_certificate", path=str(cert_path))
    now = datetime.now(timezone.utc)
    not_before = _certificate_datetime(cert, utc_name="not_valid_before_utc", legacy_name="not_valid_before")
    not_after = _certificate_datetime(cert, utc_name="not_valid_after_utc", legacy_name="not_valid_after")
    if now < (not_before if not_before.tzinfo else not_before.replace(tzinfo=timezone.utc)):
        raise RuntimeError(f"issued certificate is not valid yet: {cert_path}")
    if now >= (not_after if not_after.tzinfo else not_after.replace(tzinfo=timezone.utc)):
        raise RuntimeError(f"issued certificate is expired: {cert_path}")
    if not evidence["hostname_matches"]:
        raise RuntimeError(f"issued certificate does not cover target domain: {domain}")
    return evidence


def _expected_image_certificate(plan: ImageInstallPlan) -> dict[str, Any]:
    cert_path = _LETSENCRYPT_LIVE / plan.domain / "fullchain.pem"
    return _load_pem_certificate_identity(cert_path, domain=plan.domain)


def _peer_certificate_identity(plan: ImageInstallPlan) -> dict[str, Any]:
    """Read the certificate actually selected by local SNI, even on a failed CA check."""
    try:
        from cryptography import x509

        raw = socket.create_connection(("127.0.0.1", 443), timeout=5)
        context = ssl._create_unverified_context()
        with context.wrap_socket(raw, server_hostname=plan.domain) as tls:
            der = tls.getpeercert(binary_form=True)
        cert = x509.load_der_x509_certificate(der)
        return _certificate_identity_from_object(cert, domain=plan.domain, source="local_sni_peer", path="")
    except Exception as exc:
        return {
            "status": "unavailable",
            "source": "local_sni_peer",
            "server_name": plan.domain,
            "connect_address": "127.0.0.1",
            "reason": type(exc).__name__,
        }


def _reload_nginx() -> dict[str, Any]:
    rc, out = _run(["systemctl", "reload", "nginx"], timeout_sec=30)
    if rc != 0:
        raise RuntimeError(f"nginx reload failed rc={rc}: {_safe_runtime_detail(out)}")
    return {"status": "ok", "command": "systemctl reload nginx"}


def _run_certbot(args: list[str], *, timeout_sec: int) -> tuple[int, str]:
    deadline = time.monotonic() + _CERTBOT_LOCK_MAX_WAIT_SEC
    while True:
        rc, out = _run(args, timeout_sec=timeout_sec)
        if rc == 0 or "another instance of certbot is already running" not in out.lower():
            return rc, out
        if time.monotonic() >= deadline:
            return rc, out
        time.sleep(_CERTBOT_LOCK_RETRY_SEC)


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


def build_plan(
    *,
    image_ref: str,
    domain: str,
    php_version: str,
    instance_uid: str | None = None,
    instance_code: str | None = None,
) -> ImageInstallPlan:
    d = _domain(domain)
    uid = str(instance_uid or "").strip() or None
    code = str(instance_code or "").strip() or None
    if bool(uid) != bool(code):
        raise RuntimeError("instance_uid and instance_code must be supplied together by MCC")
    if uid:
        uid = validate_mcc_instance_uid(uid)
        code = validate_instance_code(code)
        names = instance_path_names(d, code)
        short = code
        db_name = names["db_name"]
        db_user = names["db_user"]
        webroot = Path(names["webroot"])
    else:
        short = _short(d)
        db_name = f"baza_{short}"
        db_user = f"korisnik_{short}"
        webroot = Path("/var/www") / short / "public_html"
    return ImageInstallPlan(
        image_ref=str(image_ref or "").strip(),
        domain=d,
        short_name=short,
        db_name=db_name,
        db_user=db_user,
        db_password=secrets.token_urlsafe(24),
        webroot=webroot,
        php_version=str(php_version or "").strip(),
        instance_uid=uid,
        instance_code=code,
    )


def _selected_php_bin(plan: ImageInstallPlan) -> str:
    return f"/usr/bin/php{plan.php_version}"


def _preflight(plan: ImageInstallPlan) -> list[str]:
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
    site_avail = Path("/etc/nginx/sites-available") / f"{plan.domain}.conf"
    site_enabled = Path("/etc/nginx/sites-enabled") / f"{plan.domain}.conf"
    if site_avail.exists():
        problems.append(f"nginx vhost already exists: {site_avail}")
    if site_enabled.exists() or site_enabled.is_symlink():
        problems.append(f"nginx enabled symlink already exists: {site_enabled}")
    if (_LETSENCRYPT_LIVE / plan.domain).exists() or (_LETSENCRYPT_RENEWAL / f"{plan.domain}.conf").exists():
        problems.append(f"certificate already exists: {plan.domain}")
    try:
        if _db_exists(plan.db_name):
            problems.append(f"database already exists: {plan.db_name}")
        if _db_user_exists(plan.db_user):
            problems.append(f"database user already exists: {plan.db_user}@localhost")
    except Exception as exc:
        problems.append(f"database preflight failed: {exc}")
    return problems


def _prepare_instance_parent(webroot: Path) -> None:
    parent = webroot.parent
    if parent.exists():
        if not parent.is_dir():
            raise RuntimeError(f"instance parent is not a directory: {parent}")
        if any(parent.iterdir()):
            raise RuntimeError(f"instance parent is not empty: {parent}")
        return
    parent.mkdir(parents=True, exist_ok=False)


def _rollback_failed_install(
    cfg: AgentConfig,
    plan: ImageInstallPlan,
    *,
    database_created: bool,
    user_created: bool,
    resources_started: bool,
    mail_requested: bool,
) -> list[str]:
    errors: list[str] = []
    if mail_requested:
        try:
            from mcd_agent.local_mail import disable_local_mail

            disable_local_mail(cfg, domain=plan.domain)
        except Exception as exc:
            errors.append(f"local mail: {exc}")

    if resources_started:
        for path in (
            Path("/etc/nginx/sites-enabled") / f"{plan.domain}.conf",
            Path("/etc/nginx/sites-available") / f"{plan.domain}.conf",
        ):
            try:
                if path.exists() or path.is_symlink():
                    path.unlink()
            except Exception as exc:
                errors.append(f"nginx {path}: {exc}")

        cert_dir = _LETSENCRYPT_LIVE / plan.domain
        renewal = _LETSENCRYPT_RENEWAL / f"{plan.domain}.conf"
        if cert_dir.exists() or renewal.exists():
            rc, out = _run_certbot(
                ["certbot", "delete", "--cert-name", plan.domain, "--non-interactive"],
                timeout_sec=120,
            )
            if rc != 0:
                errors.append(f"certificate: {out or 'certbot delete failed'}")
            else:
                safe_domain = re.sub(r"[^a-z0-9_.-]+", "_", plan.domain.lower()).strip("._-") or "domain"
                credential = _LETSENCRYPT_MCD / f"dns-cloudflare-{safe_domain}.ini"
                try:
                    if credential.exists():
                        credential.unlink()
                except OSError as exc:
                    errors.append(f"certificate credential: {exc}")

        try:
            if plan.webroot.exists():
                shutil.rmtree(plan.webroot)
            plan.webroot.parent.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"webroot: {exc}")

        rc, out = _run(["nginx", "-t"], timeout_sec=30)
        if rc == 0:
            reload_rc, reload_out = _run(["systemctl", "reload", "nginx"], timeout_sec=30)
            if reload_rc != 0:
                errors.append(f"nginx reload: {reload_out}")
        else:
            errors.append(f"nginx validation: {out}")

    if database_created:
        try:
            _mysql_exec(f"DROP DATABASE IF EXISTS {_quote_ident(plan.db_name)}")
        except Exception as exc:
            errors.append(f"database: {exc}")
    if user_created:
        try:
            _mysql_exec(f"DROP USER IF EXISTS {_quote_sql(plan.db_user)}@'localhost'")
        except Exception as exc:
            errors.append(f"database user: {exc}")
    return errors


def _persist_image_failure_evidence(
    cfg: AgentConfig,
    plan: ImageInstallPlan,
    *,
    failure: dict[str, Any],
    rollback_errors: list[str],
    rollback_started: bool,
) -> Path | None:
    """Persist safe acceptance evidence outside the disposable install root."""
    try:
        raw_state_path = str(getattr(cfg, "state_db_path", "") or "").strip()
        if not raw_state_path:
            return None
        state_path = Path(raw_state_path).resolve()
        directory = state_path.parent / "image-install-failures"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        suffix = re.sub(r"[^a-z0-9_.-]+", "_", plan.domain.lower()).strip("._-") or "domain"
        path = directory / f"{stamp}-{suffix}.json"
        payload = {
            "schema": "mcd-image-install-failure-evidence-v1",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "domain": plan.domain,
            "instance_uid": plan.instance_uid or None,
            "instance_code": plan.instance_code or None,
            "root": str(plan.webroot),
            "db_name": plan.db_name,
            "failure": failure,
            "rollback": {
                "started": bool(rollback_started),
                "completed": bool(rollback_started),
                "errors": [_safe_runtime_detail(item) for item in rollback_errors],
            },
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
        return path
    except Exception:
        return None


def _download(url: str, token: str, dst: Path) -> None:
    req = mcc_request(url, headers={"Authorization": f"Bearer {token}"})
    with request.urlopen(req, timeout=1800) as resp, dst.open("wb") as f:
        shutil.copyfileobj(resp, f)


def _download_json(url: str, token: str, *, timeout_sec: int = 60) -> dict[str, Any]:
    req = mcc_request(url, headers={"Authorization": f"Bearer {token}"})
    with request.urlopen(req, timeout=timeout_sec) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise RuntimeError("MCC returned invalid JSON payload")
    return payload


def _iter_mysql_import_sql(lines: Any) -> Any:
    yield from iter_mysql_import_sql(lines)


def _mysql_import_gz(path: Path, db_name: str) -> None:
    proc = subprocess.Popen(
        [*_mysql_admin_base(), db_name],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    out_b = b""
    err_b = b""
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="surrogateescape", newline="") as f:
            for chunk in _iter_mysql_import_sql(f):
                proc.stdin.write(chunk.encode("utf-8", errors="surrogateescape"))
        proc.stdin.close()
        proc.stdin = None
        out_b, err_b = proc.communicate(timeout=1800)
    except Exception as exc:
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.stdin = None
        try:
            out_b, err_b = proc.communicate(timeout=30)
        except Exception:
            proc.kill()
            out_b, err_b = proc.communicate()
        out = ((out_b or b"") + (err_b or b"")).decode("utf-8", errors="replace").strip()
        if out:
            raise RuntimeError("database import failed: " + out) from exc
        proc.kill()
        raise
    if proc.returncode != 0:
        out = ((out_b or b"") + (err_b or b"")).decode("utf-8", errors="replace").strip()
        raise RuntimeError("database import failed: " + out)


def _apt_install_packages(packages: list[str]) -> None:
    wanted = [str(x or "").strip() for x in packages if str(x or "").strip()]
    if not wanted:
        return
    if not shutil.which("apt-get"):
        raise RuntimeError("missing required packages and apt-get is unavailable: " + ", ".join(wanted))
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    for cmd, timeout in (
        (["apt-get", "update", "--allow-releaseinfo-change"], 900),
        (["apt-get", "install", "-y", "--no-install-recommends", *wanted], 1800),
    ):
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False)
        if proc.returncode != 0:
            raise RuntimeError(((proc.stdout or "") + (proc.stderr or "")).strip() or f"{cmd[0]} failed")


def _myloader_bin(cfg: AgentConfig) -> str:
    configured = str(getattr(cfg, "backup_myloader_bin", "") or "").strip()
    candidates = [configured] if configured else []
    candidates += ["myloader", "/usr/bin/myloader"]
    for name in candidates:
        if not name:
            continue
        found = shutil.which(name) or (name if Path(name).exists() else "")
        if found:
            return found
    package = str(getattr(cfg, "backup_mydumper_package", "") or "mydumper").strip() or "mydumper"
    _apt_install_packages([package])
    for name in candidates:
        if not name:
            continue
        found = shutil.which(name) or (name if Path(name).exists() else "")
        if found:
            return found
    raise RuntimeError("myloader is missing after package install")


def _mysql_admin_defaults_file() -> Path | None:
    for arg in _mysql_admin_base()[1:]:
        if arg.startswith("--defaults-extra-file="):
            return Path(arg.split("=", 1)[1])
    return None


def _myloader_base_args(cfg: AgentConfig) -> list[str]:
    args = [_myloader_bin(cfg)]
    defaults = _mysql_admin_defaults_file()
    if defaults is not None:
        args.append(f"--defaults-file={defaults}")
    return args


def _find_mydumper_dump_dir(root: Path) -> Path:
    candidates: list[Path] = []
    for item in [root, *root.rglob("*")]:
        if not item.is_dir():
            continue
        try:
            if any(child.name.startswith("metadata") for child in item.iterdir()):
                candidates.append(item)
        except Exception:
            pass
    if not candidates:
        raise RuntimeError("mydumper metadata not found in database artifact")
    candidates.sort(key=lambda p: (len(p.parts), str(p)))
    return candidates[0]


def _mysql_import_mydumper_tar(cfg: AgentConfig, path: Path, db_name: str) -> None:
    with tempfile.TemporaryDirectory(prefix="mcd-image-db-mydumper-") as td:
        extract_root = Path(td)
        with tarfile.open(path, "r:gz") as tf:
            _safe_extract(tf, extract_root)
        dump_dir = _find_mydumper_dump_dir(extract_root)
        rc, out = _run(
            [
                *_myloader_base_args(cfg),
                "-B",
                db_name,
                "-d",
                str(dump_dir),
                "--threads",
                str(max(1, int(getattr(cfg, "backup_myloader_threads", 4) or 4))),
                "--overwrite-tables",
            ],
            timeout_sec=max(1800, int(getattr(cfg, "backup_dump_timeout_sec", 1800) or 1800)),
        )
        if rc != 0:
            raise RuntimeError("myloader import failed: " + out)


def _mysql_import_artifact(cfg: AgentConfig, path: Path, db_name: str) -> None:
    if tarfile.is_tarfile(path):
        _mysql_import_mydumper_tar(cfg, path, db_name)
        return
    _mysql_import_gz(path, db_name)


def _artifact_url(cfg: AgentConfig, image_ref: str, kind: str) -> str:
    base = str(cfg.mcc_url or "").rstrip("/")
    if not base:
        raise RuntimeError("mcc.url is not configured")
    return f"{base}/api/v1/agent/mautic-images/{image_ref}/artifact/{kind}"


def _agent_url(cfg: AgentConfig, path: str, query: dict[str, str] | None = None) -> str:
    base = str(cfg.mcc_url or "").rstrip("/")
    if not base:
        raise RuntimeError("mcc.url is not configured")
    suffix = "/" + str(path or "").lstrip("/")
    if query:
        suffix += "?" + urlencode(query)
    return f"{base}{suffix}"


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


def _mautic_nginx_locations(plan: ImageInstallPlan) -> str:
    fastcgi_pass = _php_fpm_fastcgi_pass(plan.php_version)
    return f"""    location ~ \\.php$ {{
        include snippets/fastcgi-php.conf;
        fastcgi_pass {fastcgi_pass};
    }}

    location ~* ^/(?:config|vendor|node_modules|tests|var|\\.git)(?:/|$) {{
        deny all;
    }}

    location ~* ^/(?:composer\\.(?:json|lock)|package(?:-lock)?\\.json|yarn\\.lock|pnpm-lock\\.yaml|symfony\\.lock|webpack\\.config\\.js|tsconfig\\.json|phpunit\\.xml(?:\\.dist)?|codeception\\.yml|SECURITY\\.md|README(?:\\..*)?|CHANGELOG(?:\\..*)?|\\.env(?:\\..*)?)$ {{
        deny all;
    }}

    location ~* /\\.(?!well-known/) {{
        deny all;
    }}"""


def _php_fpm_fastcgi_pass(php_version: str) -> str:
    exact = Path(f"/run/php/php{str(php_version).strip()}-fpm.sock")
    if exact.exists():
        return f"unix:{exact}"
    generic = Path("/run/php/php-fpm.sock")
    if generic.exists():
        return f"unix:{generic}"
    return f"unix:{exact}"


def _http_root_location(*, ssl_enabled: bool) -> str:
    if ssl_enabled:
        return """    location / {
        return 301 https://$host$request_uri;
    }"""
    return """    location / {
        try_files $uri /index.php$is_args$args;
    }"""


def _indented_form_embed_location(fastcgi_pass: str) -> str:
    rendered = render_form_embed_location(fastcgi_pass=fastcgi_pass)
    return "\n".join(("    " + line) if line else "" for line in rendered.splitlines())


def _ssl_server_block(plan: ImageInstallPlan, web_root: Path) -> str:
    cert_dir = Path("/etc/letsencrypt/live") / plan.domain
    return f"""server {{
    listen 443 ssl;
    server_name {plan.domain};
    root {web_root};
    index index.php index.html;
    client_max_body_size 128M;

    ssl_certificate {cert_dir / "fullchain.pem"};
    ssl_certificate_key {cert_dir / "privkey.pem"};

    location / {{
        try_files $uri /index.php$is_args$args;
    }}

{_indented_form_embed_location(_php_fpm_fastcgi_pass(plan.php_version))}

{_mautic_nginx_locations(plan)}
}}
"""


def _write_nginx_vhost(plan: ImageInstallPlan, *, ssl_enabled: bool = False) -> Path:
    site = Path("/etc/nginx/sites-available") / f"{plan.domain}.conf"
    site.parent.mkdir(parents=True, exist_ok=True)
    web_root = _nginx_web_root(plan.webroot)
    content = render_nginx_template(
        "mautic_image_vhost.conf",
        DOMAIN=plan.domain,
        WEB_ROOT=web_root,
        HTTP_ROOT_LOCATION=_http_root_location(ssl_enabled=ssl_enabled),
        FORM_EMBED_HTTP_LOCATION=(
            "" if ssl_enabled else _indented_form_embed_location(_php_fpm_fastcgi_pass(plan.php_version))
        ),
        MAUTIC_DENY_LOCATIONS=_mautic_nginx_locations(plan),
        SSL_SERVER_BLOCK=_ssl_server_block(plan, web_root) if ssl_enabled else "",
    )
    site.write_text(content, encoding="utf-8")
    enabled = Path("/etc/nginx/sites-enabled") / site.name
    enabled.parent.mkdir(parents=True, exist_ok=True)
    if not enabled.exists() and not enabled.is_symlink():
        enabled.symlink_to(site)
    return site


def _nginx_web_root(project_root: Path) -> Path:
    """Return the actual nginx root for extracted zip/composer image layouts."""
    root = Path(project_root)
    for child in ("docroot", "public"):
        candidate = root / child
        if (candidate / "index.php").exists():
            return candidate
    return root


def _certbot_cloudflare_credentials_url(cfg: AgentConfig, plan: ImageInstallPlan, credential_ref: str) -> str:
    return _agent_url(
        cfg,
        "/api/v1/agent/dns-credentials/certbot-cloudflare",
        {"domain": plan.domain, "credential_ref": credential_ref},
    )


def _certbot_cloudflare_credentials_path(cfg: AgentConfig, plan: ImageInstallPlan, credential_ref: str) -> Path:
    payload = _download_json(_certbot_cloudflare_credentials_url(cfg, plan, credential_ref), str(cfg.mcc_token), timeout_sec=60)
    token = str(payload.get("api_token") or "").strip()
    if not token:
        raise RuntimeError("Cloudflare DNS credential has no API token")
    safe_domain = re.sub(r"[^a-z0-9_.-]+", "_", plan.domain.lower()).strip("._-") or "domain"
    cred_dir = _LETSENCRYPT_MCD
    cred_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cred_dir, 0o700)
    path = cred_dir / f"dns-cloudflare-{safe_domain}.ini"
    path.write_text("dns_cloudflare_api_token = " + token + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _ensure_certbot_cloudflare_plugin() -> None:
    rc, out = _run(["certbot", "plugins"], timeout_sec=60)
    if rc == 0 and "dns-cloudflare" in out:
        return
    _apt_install_packages(["python3-certbot-dns-cloudflare"])
    rc, out = _run(["certbot", "plugins"], timeout_sec=60)
    if rc != 0 or "dns-cloudflare" not in out:
        raise RuntimeError("certbot dns-cloudflare plugin is not available: " + out)


def _run_certbot_http01(plan: ImageInstallPlan) -> None:
    rc, out = _run_certbot(
        [
            "certbot",
            "--nginx",
            "-d",
            plan.domain,
            "--non-interactive",
            "--agree-tos",
            "--redirect",
            "--register-unsafely-without-email",
        ],
        timeout_sec=600,
    )
    if rc != 0:
        raise RuntimeError("certbot failed: " + out)


def _run_certbot_cloudflare_dns01(cfg: AgentConfig, plan: ImageInstallPlan, credential_ref: str) -> None:
    _ensure_certbot_cloudflare_plugin()
    credentials_path = _certbot_cloudflare_credentials_path(cfg, plan, credential_ref)
    rc, out = _run_certbot(
        [
            "certbot",
            "certonly",
            "--dns-cloudflare",
            "--dns-cloudflare-credentials",
            str(credentials_path),
            "--dns-cloudflare-propagation-seconds",
            "60",
            "--cert-name",
            plan.domain,
            "-d",
            plan.domain,
            "--non-interactive",
            "--agree-tos",
            "--keep-until-expiring",
            "--register-unsafely-without-email",
        ],
        timeout_sec=900,
    )
    if rc != 0:
        raise RuntimeError("certbot dns-cloudflare failed: " + out)
    _expected_image_certificate(plan)
    _write_nginx_vhost(plan, ssl_enabled=True)
    rc, out = _run(["nginx", "-t"], timeout_sec=30)
    if rc != 0:
        raise RuntimeError("nginx -t failed after SSL vhost update: " + _safe_runtime_detail(out))
    _reload_nginx()


def _skip_image_archive_member(name: str) -> bool:
    normalized = str(name or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized == ".mcd" or normalized.startswith(".mcd/")


def _safe_extract(tar: tarfile.TarFile, dst: Path) -> None:
    root = dst.resolve()
    members: list[tarfile.TarInfo] = []
    for member in tar.getmembers():
        if _skip_image_archive_member(member.name):
            continue
        target = (dst / member.name).resolve()
        if root != target and root not in target.parents:
            raise RuntimeError(f"unsafe path in image archive: {member.name}")
        if member.issym() or member.islnk():
            link_target = (target.parent / member.linkname).resolve()
            if root != link_target and root not in link_target.parents:
                raise RuntimeError(f"unsafe link in image archive: {member.name} -> {member.linkname}")
        members.append(member)
    tar.extractall(dst, members=members)


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
    """Probe the activated local vhost with SNI, CA validation and bounded readiness."""
    import hashlib

    with tempfile.TemporaryDirectory(prefix="mcd-image-https-") as td:
        body = Path(td) / "login.html"
        last: dict[str, Any] = {
            "server_name": plan.domain,
            "connect_address": "127.0.0.1",
            "expected_certificate": str(_LETSENCRYPT_LIVE / plan.domain / "fullchain.pem"),
        }
        for attempt in range(1, _IMAGE_HTTPS_ATTEMPTS + 1):
            if body.exists():
                body.unlink()
            rc, output = _run(
                ["curl", "--silent", "--show-error", "--noproxy", "*",
                 "--connect-timeout", "5", "--max-time", "30",
                 "--resolve", f"{plan.domain}:443:127.0.0.1",
                 "--output", str(body), "--write-out", "MCD_HTTP_STATUS:%{http_code}",
                 f"https://{plan.domain}/s/login"], timeout_sec=40,
            )
            marker = re.search(r"MCD_HTTP_STATUS:(\d{3})", str(output or ""))
            status = marker.group(1) if marker else (str(output or "").strip() if re.fullmatch(r"\d{3}", str(output or "").strip()) else "")
            peer = _peer_certificate_identity(plan)
            detail = _safe_runtime_detail(re.sub(r"MCD_HTTP_STATUS:\d{3}", "", str(output or "")))
            last = {
                "attempt": attempt,
                "server_name": plan.domain,
                "connect_address": "127.0.0.1",
                "curl_rc": int(rc),
                "http_status": status,
                "stderr": detail,
                "peer_certificate": peer,
                "expected_certificate": str(_LETSENCRYPT_LIVE / plan.domain / "fullchain.pem"),
            }
            if rc == 0 and status == "200":
                html = body.read_bytes() if body.is_file() else b""
                if not re.search(rb"type\s*=\s*[\"']password[\"']", html, re.IGNORECASE):
                    last["failure_code"] = "login_form_missing"
                elif peer.get("status") != "ok" or not bool(peer.get("hostname_matches")):
                    last["failure_code"] = "tls_peer_certificate_mismatch"
                else:
                    return {
                        "status": "ok",
                        "url": f"https://{plan.domain}/s/login",
                        "http_status": 200,
                        "connect_address": "127.0.0.1",
                        "server_name": plan.domain,
                        "tls_verified": True,
                        "login_form": True,
                        "body_sha256": hashlib.sha256(html).hexdigest(),
                        "peer_certificate": peer,
                        "attempts": attempt,
                    }
            elif int(rc) == 60:
                last["failure_code"] = "tls_certificate_mismatch"
            elif int(rc) != 0:
                last["failure_code"] = "tls_connection_failed"
            else:
                last["failure_code"] = "http_status_not_200"
            if attempt < _IMAGE_HTTPS_ATTEMPTS:
                time.sleep(_IMAGE_HTTPS_RETRY_SEC)

        failure_code = str(last.get("failure_code") or "image_https_verification_failed")
        message = (
            f"image HTTPS login verification failed code={failure_code} "
            f"rc={last.get('curl_rc', '')} http={last.get('http_status', '') or '000'} "
            f"server_name={plan.domain} peer={last.get('peer_certificate', {}).get('dns_names', [])}"
        )
        raise ImageHTTPSVerificationError(message, last)


@stable_source_operation
def install_from_image(
    cfg: AgentConfig,
    *,
    image_ref: str,
    domain: str,
    php_version: str,
    yes: bool = False,
    run_certbot: bool = True,
    certbot_dns_credential_ref: str | None = None,
    own_mail: bool = False,
    mail_profile_id: str | None = None,
    instance_uid: str | None = None,
    instance_code: str | None = None,
) -> dict[str, Any]:
    plan = build_plan(
        image_ref=image_ref,
        domain=domain,
        php_version=php_version,
        instance_uid=instance_uid,
        instance_code=instance_code,
    )
    if not yes:
        raise RuntimeError("--yes is required")
    if not str(cfg.mcc_token or "").strip():
        raise RuntimeError("mcc.token is not configured")

    print(f"Preflight: domain={plan.domain} webroot={plan.webroot} db={plan.db_name}")
    problems = _preflight(plan)
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

        database_created = False
        user_created = False
        resources_started = False
        mail_requested = bool(mail_profile_id or own_mail)
        try:
            print("Creating database and user")
            _mysql_exec(f"CREATE DATABASE {_quote_ident(plan.db_name)} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            database_created = True
            _mysql_exec(
                "CREATE USER "
                + _quote_sql(plan.db_user)
                + "@'localhost' IDENTIFIED BY "
                + _quote_sql(plan.db_password)
            )
            user_created = True
            _mysql_exec(f"GRANT ALL PRIVILEGES ON {_quote_ident(plan.db_name)}.* TO {_quote_sql(plan.db_user)}@'localhost'")
            _mysql_exec("FLUSH PRIVILEGES")

            print("Extracting files")
            _prepare_instance_parent(plan.webroot)
            resources_started = True
            plan.webroot.mkdir(parents=True, exist_ok=False)
            with tarfile.open(files_tgz, "r:gz") as tf:
                _safe_extract(tf, plan.webroot)

            local_php = plan.webroot / "config" / "local.php"
            if not local_php.exists():
                local_php = plan.webroot / "app" / "config" / "local.php"
            if not local_php.exists():
                raise RuntimeError("local.php not found after extraction")
            print("Patching Mautic local.php")
            _patch_local_php(local_php, plan)
            if plan.instance_uid and plan.instance_code:
                write_instance_identity(
                    plan.webroot,
                    instance_uid=plan.instance_uid,
                    instance_code=plan.instance_code,
                    primary_domain=plan.domain,
                )

            print("Importing database")
            _mysql_import_artifact(cfg, db_artifact, plan.db_name)

            print("Fixing permissions")
            permission_rc, _ = _run(["chown", "-R", "www-data:www-data", str(plan.webroot.parent)], timeout_sec=300)
            if permission_rc != 0:
                raise RuntimeError("image runtime ownership setup failed")
            for rel in ("var/cache", "var/logs"):
                imported = plan.webroot / rel
                if imported.is_symlink():
                    imported.unlink()
                elif imported.exists():
                    shutil.rmtree(imported)

            print("Warming prod runtime before publishing nginx vhost")
            warmup = _warm_image_runtime(plan)

            print("Writing nginx vhost")
            _write_nginx_vhost(plan)
            rc, out = _run(["nginx", "-t"], timeout_sec=30)
            if rc != 0:
                raise RuntimeError("nginx -t failed: " + out)
            _reload_nginx()

            if run_certbot:
                dns_ref = str(certbot_dns_credential_ref or "").strip()
                if dns_ref:
                    print("Running certbot via Cloudflare DNS-01")
                    _run_certbot_cloudflare_dns01(cfg, plan, dns_ref)
                else:
                    print("Running certbot via nginx HTTP-01")
                    _run_certbot_http01(plan)

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
            failure = getattr(exc, "evidence", None)
            if not isinstance(failure, dict):
                failure = {
                    "failure_code": type(exc).__name__,
                    "message": _safe_runtime_detail(str(exc)),
                    "server_name": plan.domain,
                    "expected_certificate": str(_LETSENCRYPT_LIVE / plan.domain / "fullchain.pem"),
                }
            evidence_path = _persist_image_failure_evidence(
                cfg,
                plan,
                failure=failure,
                rollback_errors=[],
                rollback_started=False,
            )
            rollback_errors = _rollback_failed_install(
                cfg,
                plan,
                database_created=database_created,
                user_created=user_created,
                resources_started=resources_started,
                mail_requested=mail_requested,
            )
            final_evidence_path = _persist_image_failure_evidence(
                cfg,
                plan,
                failure=failure,
                rollback_errors=rollback_errors,
                rollback_started=True,
            ) or evidence_path
            evidence_note = f"; failure evidence: {final_evidence_path}" if final_evidence_path else ""
            if rollback_errors:
                raise RuntimeError(f"{exc}; rollback errors: {'; '.join(rollback_errors)}{evidence_note}") from exc
            if evidence_note:
                raise RuntimeError(f"{exc}{evidence_note}") from exc
            raise

    from mcd_agent.inventory import InstanceInventory

    inv = InstanceInventory(cfg.state_db_path)
    count = inv.rescan(cfg)
    print(f"Rescan complete: {count} instances")
    actual = identity_readback(
        plan.webroot,
        instance_uid=plan.instance_uid,
        instance_code=plan.instance_code,
        primary_domain=plan.domain,
    )
    return {
        "status": "ok",
        "domain": plan.domain,
        "webroot": str(plan.webroot),
        "db_name": plan.db_name,
        "php_version": plan.php_version,
        "mail_mode": "own_host" if own_mail or mail_profile_id else "external",
        "mail_profile_id": str(mail_profile_id or ""),
        "instances": count,
        "instance_uid": actual["instance_uid"] or None,
        "instance_code": actual["instance_code"] or None,
        "actual_state": {
            "instance_uid": actual["instance_uid"] or None,
            "instance_code": actual["instance_code"] or None,
            "root": actual["root"],
            "webroot": str(plan.webroot),
            "db_name": plan.db_name,
            "db_user": plan.db_user,
        },
        "runtime_warmup": warmup,
        "runtime_verification": runtime_verification,
    }
