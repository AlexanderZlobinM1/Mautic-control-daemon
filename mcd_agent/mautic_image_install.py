from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import gzip
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

from mcd_agent.config import AgentConfig
from mcd_agent.inventory import InstanceInventory


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


def _preflight(plan: ImageInstallPlan) -> list[str]:
    problems: list[str] = []
    if os.geteuid() != 0:
        problems.append("must run as root")
    if not re.match(r"^[0-9]+\.[0-9]+$", plan.php_version):
        problems.append("invalid php version")
    for cmd in ("tar", "gzip", "certbot", "nginx", "php", "composer", "node", "npm"):
        if not shutil.which(cmd):
            problems.append(f"missing command: {cmd}")
    php_sock = Path(f"/run/php/php{plan.php_version}-fpm.sock")
    if not php_sock.exists():
        problems.append(f"missing PHP-FPM socket: {php_sock}")
    if plan.webroot.exists():
        problems.append(f"webroot already exists: {plan.webroot}")
    site_avail = Path("/etc/nginx/sites-available") / f"{plan.domain}.conf"
    site_enabled = Path("/etc/nginx/sites-enabled") / f"{plan.domain}.conf"
    if site_avail.exists():
        problems.append(f"nginx vhost already exists: {site_avail}")
    if site_enabled.exists() or site_enabled.is_symlink():
        problems.append(f"nginx enabled symlink already exists: {site_enabled}")
    try:
        if _db_exists(plan.db_name):
            problems.append(f"database already exists: {plan.db_name}")
    except Exception as exc:
        problems.append(f"database preflight failed: {exc}")
    return problems


def _download(url: str, token: str, dst: Path) -> None:
    req = request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with request.urlopen(req, timeout=1800) as resp, dst.open("wb") as f:
        shutil.copyfileobj(resp, f)


_CREATE_TABLE_RE = re.compile(r"^CREATE TABLE `([^`]+)`")
_CREATE_COLUMN_RE = re.compile(r"^\s*`([^`]+)`\s+")
_INSERT_VALUES_RE = re.compile(r"^INSERT INTO `([^`]+)` VALUES\s*", re.DOTALL)


def _sql_statement_complete(sql: str) -> bool:
    in_quote = False
    escaped = False
    complete = False
    for ch in sql:
        if in_quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                in_quote = False
            continue
        if ch == "'":
            in_quote = True
            continue
        if ch == ";":
            complete = True
        elif not ch.isspace():
            complete = False
    return complete and not in_quote


def _split_sql_rows(values_sql: str) -> list[str]:
    rows: list[str] = []
    i = 0
    n = len(values_sql)
    while i < n:
        while i < n and (values_sql[i].isspace() or values_sql[i] in ",;"):
            i += 1
        if i >= n:
            break
        if values_sql[i] != "(":
            return []
        start = i + 1
        i += 1
        depth = 1
        in_quote = False
        escaped = False
        while i < n:
            ch = values_sql[i]
            if in_quote:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == "'":
                    in_quote = False
            else:
                if ch == "'":
                    in_quote = True
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        rows.append(values_sql[start:i])
                        i += 1
                        break
            i += 1
        else:
            return []
    return rows


def _split_sql_values(row_sql: str) -> list[str]:
    values: list[str] = []
    start = 0
    depth = 0
    in_quote = False
    escaped = False
    for i, ch in enumerate(row_sql):
        if in_quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                in_quote = False
            continue
        if ch == "'":
            in_quote = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            values.append(row_sql[start:i])
            start = i + 1
    values.append(row_sql[start:])
    return values


def _rewrite_generated_insert(stmt: str, generated_columns: dict[str, tuple[list[str], set[int]]]) -> str:
    match = _INSERT_VALUES_RE.match(stmt)
    if not match:
        return stmt
    table = match.group(1)
    meta = generated_columns.get(table)
    if not meta:
        return stmt
    columns, generated_indexes = meta
    non_generated_columns = [name for idx, name in enumerate(columns) if idx not in generated_indexes]
    rows = _split_sql_rows(stmt[match.end() :])
    if not rows:
        return stmt
    rewritten_rows: list[str] = []
    for row in rows:
        values = _split_sql_values(row)
        if len(values) != len(columns):
            return stmt
        kept = [value for idx, value in enumerate(values) if idx not in generated_indexes]
        rewritten_rows.append("(" + ",".join(kept) + ")")
    column_sql = ",".join(_quote_ident(col) for col in non_generated_columns)
    return f"INSERT INTO {_quote_ident(table)} ({column_sql}) VALUES\n" + ",\n".join(rewritten_rows) + ";\n"


def _iter_mysql_import_sql(lines: Any) -> Any:
    generated_columns: dict[str, tuple[list[str], set[int]]] = {}
    create_table = ""
    create_columns: list[tuple[str, bool]] = []
    insert_buffer: list[str] = []

    for line in lines:
        if insert_buffer:
            insert_buffer.append(line)
            stmt = "".join(insert_buffer)
            if _sql_statement_complete(stmt):
                yield _rewrite_generated_insert(stmt, generated_columns)
                insert_buffer = []
            continue

        create_match = _CREATE_TABLE_RE.match(line)
        if create_match:
            create_table = create_match.group(1)
            create_columns = []
            yield line
            continue

        if create_table:
            column_match = _CREATE_COLUMN_RE.match(line)
            if column_match:
                create_columns.append((column_match.group(1), "GENERATED" in line.upper()))
            if line.startswith(")"):
                indexes = {idx for idx, (_, generated) in enumerate(create_columns) if generated}
                if indexes:
                    generated_columns[create_table] = ([name for name, _ in create_columns], indexes)
                create_table = ""
                create_columns = []
            yield line
            continue

        insert_match = _INSERT_VALUES_RE.match(line)
        if insert_match and insert_match.group(1) in generated_columns:
            insert_buffer = [line]
            stmt = "".join(insert_buffer)
            if _sql_statement_complete(stmt):
                yield _rewrite_generated_insert(stmt, generated_columns)
                insert_buffer = []
            continue

        yield line

    if insert_buffer:
        yield _rewrite_generated_insert("".join(insert_buffer), generated_columns)


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
        (["apt-get", "update"], 900),
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


def _write_nginx_vhost(plan: ImageInstallPlan) -> Path:
    site = Path("/etc/nginx/sites-available") / f"{plan.domain}.conf"
    site.parent.mkdir(parents=True, exist_ok=True)
    web_root = _nginx_web_root(plan.webroot)
    content = f"""server {{
    listen 80;
    listen [::]:80;
    server_name {plan.domain};
    root {web_root};
    index index.php index.html;
    client_max_body_size 128M;

    location / {{
        try_files $uri /index.php$is_args$args;
    }}

    location ~ \\.php$ {{
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php{plan.php_version}-fpm.sock;
    }}

    location ~* ^/(?:config|vendor|node_modules|tests|var|\\.git)(?:/|$) {{
        deny all;
    }}

    location ~* ^/(?:composer\\.(?:json|lock)|package(?:-lock)?\\.json|yarn\\.lock|pnpm-lock\\.yaml|symfony\\.lock|webpack\\.config\\.js|tsconfig\\.json|phpunit\\.xml(?:\\.dist)?|codeception\\.yml|SECURITY\\.md|README(?:\\..*)?|CHANGELOG(?:\\..*)?|\\.env(?:\\..*)?)$ {{
        deny all;
    }}

    location ~* /\\.(?!well-known/) {{
        deny all;
    }}
}}
"""
    site.write_text(content, encoding="utf-8")
    enabled = Path("/etc/nginx/sites-enabled") / site.name
    enabled.parent.mkdir(parents=True, exist_ok=True)
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


def _safe_extract(tar: tarfile.TarFile, dst: Path) -> None:
    root = dst.resolve()
    for member in tar.getmembers():
        target = (dst / member.name).resolve()
        if root != target and root not in target.parents:
            raise RuntimeError(f"unsafe path in image archive: {member.name}")
        if member.issym() or member.islnk():
            link_target = (target.parent / member.linkname).resolve()
            if root != link_target and root not in link_target.parents:
                raise RuntimeError(f"unsafe link in image archive: {member.name} -> {member.linkname}")
    tar.extractall(dst)


def install_from_image(
    cfg: AgentConfig,
    *,
    image_ref: str,
    domain: str,
    php_version: str,
    yes: bool = False,
    run_certbot: bool = True,
) -> dict[str, Any]:
    plan = build_plan(image_ref=image_ref, domain=domain, php_version=php_version)
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

        print("Creating database and user")
        _mysql_exec(f"CREATE DATABASE {_quote_ident(plan.db_name)} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        _mysql_exec(
            "CREATE USER IF NOT EXISTS "
            + _quote_sql(plan.db_user)
            + "@'localhost' IDENTIFIED BY "
            + _quote_sql(plan.db_password)
        )
        _mysql_exec(f"GRANT ALL PRIVILEGES ON {_quote_ident(plan.db_name)}.* TO {_quote_sql(plan.db_user)}@'localhost'")
        _mysql_exec("FLUSH PRIVILEGES")

        print("Extracting files")
        plan.webroot.parent.mkdir(parents=True, exist_ok=False)
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

        print("Importing database")
        _mysql_import_artifact(cfg, db_artifact, plan.db_name)

        print("Fixing permissions")
        _run(["chown", "-R", "www-data:www-data", str(plan.webroot.parent)], timeout_sec=300)
        for rel in ("var/cache", "var/logs"):
            shutil.rmtree(plan.webroot / rel, ignore_errors=True)

        print("Writing nginx vhost")
        _write_nginx_vhost(plan)
        rc, out = _run(["nginx", "-t"], timeout_sec=30)
        if rc != 0:
            raise RuntimeError("nginx -t failed: " + out)
        _run(["systemctl", "reload", "nginx"], timeout_sec=30)

        if run_certbot:
            print("Running certbot")
            rc, out = _run(
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

    inv = InstanceInventory(cfg.state_db_path)
    count = inv.rescan(cfg)
    print(f"Rescan complete: {count} instances")
    return {
        "status": "ok",
        "domain": plan.domain,
        "webroot": str(plan.webroot),
        "db_name": plan.db_name,
        "php_version": plan.php_version,
        "instances": count,
    }
