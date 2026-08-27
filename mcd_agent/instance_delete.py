from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

from mcd_agent.config import AgentConfig
from mcd_agent.localphp import parse_local_php
from mcd_agent.models import DBConfig


_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_$-]{1,64}$")
_LOCAL_DB_HOSTS = {"", "localhost", "127.0.0.1", "::1"}
_NGINX_DIRS = (Path("/etc/nginx/sites-enabled"), Path("/etc/nginx/sites-available"))
_VAR_WWW = Path("/var/www")
_LETSENCRYPT_LIVE = Path("/etc/letsencrypt/live")
_LETSENCRYPT_RENEWAL = Path("/etc/letsencrypt/renewal")
_LETSENCRYPT_MCD = Path("/etc/letsencrypt/mcd")


def _nginx_dir(name: str) -> Path:
    for directory in _NGINX_DIRS:
        if directory.name == name:
            return directory
    return Path("/etc/nginx") / name


@dataclass
class InstanceDeletePlan:
    root: Path
    local_php: Path | None
    domains: list[str]
    nginx_paths: list[Path]
    db_name: str
    db_host: str
    db_port: str
    db_user: str
    db_password: str
    delete_files: bool
    delete_vhost: bool
    delete_db: bool


def _run(
    args: list[str],
    *,
    timeout_sec: int = 120,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    proc = subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
        env=env,
    )
    return int(proc.returncode), ((proc.stdout or "") + (proc.stderr or "")).strip()


def _mysql_bin() -> str:
    for name in ("mariadb", "mysql"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("mysql/mariadb client is missing")


def _mysql_exec(
    sql: str,
    *,
    timeout_sec: int = 120,
    host: str = "",
    port: str = "",
    user: str = "",
    password: str = "",
) -> str:
    args = [_mysql_bin(), "-N", "-B"]
    clean_host = str(host or "").strip()
    clean_port = str(port or "").strip()
    clean_user = str(user or "").strip()
    if clean_user:
        args.extend(["-u", clean_user])
    if clean_host and clean_host != "localhost":
        args.extend(["-h", clean_host])
    if clean_port and clean_host and clean_host != "localhost":
        args.extend(["-P", clean_port])
    args.extend(["-e", sql])
    env = None
    if password:
        env = dict(os.environ)
        env["MYSQL_PWD"] = str(password)
    rc, out = _run(args, timeout_sec=timeout_sec, env=env)
    if rc != 0:
        raise RuntimeError(out or "mysql command failed")
    return out


def _quote_ident(value: str) -> str:
    return "`" + str(value).replace("`", "``") + "`"


def _quote_sql(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _clean_domain(raw: str) -> str:
    domain = str(raw or "").strip().lower().rstrip(".")
    if not domain:
        return ""
    if not _DOMAIN_RE.match(domain):
        raise RuntimeError(f"invalid domain: {raw}")
    return domain


def _domain_from_url(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I) else "https://" + text)
    host = str(parsed.hostname or "").strip().lower()
    return _clean_domain(host) if host else ""


def _safe_root(raw: str) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise RuntimeError("--root is required")
    root = Path(text)
    if not root.is_absolute():
        raise RuntimeError("root must be an absolute path")
    resolved = root.resolve(strict=False)
    var_www = Path("/var/www").resolve(strict=False)
    if resolved == Path("/") or resolved == var_www or var_www not in resolved.parents:
        raise RuntimeError(f"unsafe root outside /var/www: {resolved}")
    rel_parts = resolved.relative_to(var_www).parts
    if not rel_parts:
        raise RuntimeError(f"unsafe root is too broad: {resolved}")
    if len(rel_parts) == 1 and not ((resolved / "config" / "local.php").exists() or (resolved / "app" / "config" / "local.php").exists()):
        raise RuntimeError(f"unsafe one-level root without local.php: {resolved}")
    return resolved


def _local_php_path(root: Path) -> Path | None:
    for rel in ("config/local.php", "app/config/local.php"):
        path = root / rel
        if path.exists() and path.is_file():
            return path
    return None


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _safe_nginx_child(path: Path) -> bool:
    try:
        parent = path.parent.resolve(strict=False)
    except Exception:
        return False
    return parent in {d.resolve(strict=False) for d in _NGINX_DIRS}


def _read_nginx_text(path: Path) -> str:
    target = path
    if path.is_symlink():
        try:
            target = path.resolve(strict=True)
        except Exception:
            return ""
    if not target.exists() or not target.is_file():
        return ""
    try:
        return target.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _nginx_server_names(text: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(r"(?im)^\s*server_name\s+([^;]+);", text):
        for raw in re.split(r"\s+", match.group(1).strip()):
            name = raw.strip().lower().rstrip(".")
            if not name or name == "_" or name.startswith("~"):
                continue
            names.add(name)
    return names


def _nginx_matches_domain(path: Path, domain_set: set[str]) -> bool:
    if not domain_set:
        return False
    text = _read_nginx_text(path)
    if text:
        names = _nginx_server_names(text)
        if names.intersection(domain_set):
            return True
    return path.name.lower() in {f"{domain}.conf" for domain in domain_set}


def _nginx_candidates(root: Path, domains: list[str]) -> list[Path]:
    wanted: list[Path] = []
    domain_set = {d.lower() for d in domains if d}
    if not domain_set:
        return []
    enabled_root = _nginx_dir("sites-enabled")
    for domain in sorted(domain_set):
        enabled = enabled_root / f"{domain}.conf"
        if (enabled.exists() or enabled.is_symlink()) and _nginx_matches_domain(enabled, domain_set):
            wanted.append(enabled)

    for base in _NGINX_DIRS:
        if not base.exists() or not base.is_dir():
            continue
        for item in base.iterdir():
            if not item.is_file() and not item.is_symlink():
                continue
            if _nginx_matches_domain(item, domain_set):
                wanted.append(item)
    return [p for p in _dedupe_paths(wanted) if _safe_nginx_child(p)]


def _disable_nginx_vhost(path: Path) -> tuple[bool, str]:
    if not (path.exists() or path.is_symlink()):
        return False, f"nginx vhost already absent: {path}"
    enabled_dir = _nginx_dir("sites-enabled").resolve(strict=False)
    available_dir = _nginx_dir("sites-available").resolve(strict=False)
    try:
        parent = path.parent.resolve(strict=False)
    except Exception:
        raise RuntimeError(f"unsafe nginx path: {path}")
    if parent not in {enabled_dir, available_dir}:
        raise RuntimeError(f"unsafe nginx path: {path}")
    if parent == enabled_dir:
        path.unlink()
        return True, f"removed enabled nginx config: {path}"
    path.unlink()
    return True, f"removed available nginx config: {path}"


def _inventory_db_for_root(cfg: AgentConfig | None, root: Path) -> DBConfig | None:
    state_db_path = str(getattr(cfg, "state_db_path", "") or "").strip()
    if not state_db_path:
        return None
    try:
        from mcd_agent.inventory import InstanceInventory

        inventory = InstanceInventory(state_db_path)
        for inst in inventory.list_instances():
            if Path(inst.root).resolve(strict=False) == root and inst.db is not None:
                return inst.db
    except Exception:
        return None
    return None


def build_delete_plan(
    *,
    cfg: AgentConfig | None = None,
    root: str,
    domains: list[str] | None = None,
    db_name: str | None = None,
    delete_files: bool = False,
    delete_vhost: bool = False,
    delete_db: bool = False,
) -> InstanceDeletePlan:
    target_root = _safe_root(root)
    local_php = _local_php_path(target_root)
    local_cfg: dict[str, str] = {}
    if local_php is not None:
        local_cfg = parse_local_php(str(local_php))

    cleaned_domains: list[str] = []
    seen_domains: set[str] = set()
    for raw_domain in list(domains or []):
        d = _clean_domain(raw_domain)
        if d and d not in seen_domains:
            seen_domains.add(d)
            cleaned_domains.append(d)
    site_domain = _domain_from_url(local_cfg.get("site_url", ""))
    if site_domain and site_domain not in seen_domains:
        cleaned_domains.append(site_domain)

    inventory_db = _inventory_db_for_root(cfg, target_root) if delete_db else None
    explicit_db_name = str(db_name or "").strip()
    final_db_name = str(explicit_db_name or local_cfg.get("db_name", "") or (inventory_db.name if inventory_db else "") or "").strip()
    db_host = str(local_cfg.get("db_host", "") or (inventory_db.host if inventory_db else "") or "").strip().lower()
    db_port = str(local_cfg.get("db_port", "") or (inventory_db.port if inventory_db else "") or "").strip()
    db_user = str(local_cfg.get("db_user", "") or (inventory_db.user if inventory_db else "") or "").strip()
    db_password = str(local_cfg.get("db_password", "") or (inventory_db.password if inventory_db else "") or "")
    if delete_db:
        if not final_db_name:
            raise RuntimeError("database name is unavailable; local.php is missing or incomplete")
        if not _DB_NAME_RE.match(final_db_name):
            raise RuntimeError(f"unsafe database name: {final_db_name}")
        if db_host not in _LOCAL_DB_HOSTS:
            raise RuntimeError(f"refusing to drop non-local database host: {db_host}")
        if not db_user and not explicit_db_name:
            raise RuntimeError("database user is unavailable; local.php is missing or incomplete")

    nginx_paths = _nginx_candidates(target_root, cleaned_domains) if delete_vhost else []
    return InstanceDeletePlan(
        root=target_root,
        local_php=local_php,
        domains=cleaned_domains,
        nginx_paths=nginx_paths,
        db_name=final_db_name,
        db_host=db_host,
        db_port=db_port,
        db_user=db_user,
        db_password=db_password,
        delete_files=bool(delete_files),
        delete_vhost=bool(delete_vhost),
        delete_db=bool(delete_db),
    )


def _plan_public(plan: InstanceDeletePlan) -> dict[str, Any]:
    return {
        "root": str(plan.root),
        "local_php": str(plan.local_php) if plan.local_php else "",
        "domains": list(plan.domains),
        "nginx_paths": [str(p) for p in plan.nginx_paths],
        "db_name": plan.db_name,
        "db_host": plan.db_host or "localhost",
        "db_port": plan.db_port or "3306",
        "db_user": plan.db_user,
        "delete_files": plan.delete_files,
        "delete_vhost": plan.delete_vhost,
        "delete_db": plan.delete_db,
    }


def _chmod_tree_for_delete(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except Exception:
        pass
    try:
        walker = os.walk(path, topdown=False, onerror=lambda _exc: None)
        for root, dirs, files in walker:
            root_path = Path(root)
            for name in files:
                try:
                    os.chmod(root_path / name, 0o600)
                except Exception:
                    pass
            for name in dirs:
                try:
                    os.chmod(root_path / name, 0o700)
                except Exception:
                    pass
            try:
                os.chmod(root_path, 0o700)
            except Exception:
                pass
    except Exception:
        pass


def _remaining_tree_entries(path: Path, *, limit: int = 20) -> list[str]:
    out: list[str] = []
    try:
        for child in path.rglob("*"):
            try:
                out.append(str(child.relative_to(path)))
            except Exception:
                out.append(str(child))
            if len(out) >= limit:
                break
    except Exception:
        return out
    return out


def _quarantine_instance_root(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    base = path.parent / f".mcd-delete-{path.name}-{stamp}-{os.getpid()}"
    for idx in range(100):
        target = base if idx == 0 else path.parent / f"{base.name}-{idx}"
        try:
            path.rename(target)
            return target
        except FileExistsError:
            continue
    raise RuntimeError(f"failed to allocate delete quarantine path for {path}")


def _remove_instance_root(path: Path, *, attempts: int = 6, sleep_sec: float = 0.2) -> None:
    last_error: Exception | None = None
    target = path
    for attempt in range(max(1, int(attempts))):
        if not path.exists() and not target.exists():
            return
        if target == path and path.exists():
            _chmod_tree_for_delete(path)
            try:
                target = _quarantine_instance_root(path)
            except FileNotFoundError:
                return
            except OSError as exc:
                last_error = exc
                target = path
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            if not path.exists():
                return
            target = path
            continue
        except OSError as exc:
            last_error = exc
            _chmod_tree_for_delete(target)
            time.sleep(float(sleep_sec) * float(attempt + 1))
            continue
        if not target.exists() and not path.exists():
            return
        if not target.exists() and path.exists():
            target = path
            continue
        last_error = OSError(f"directory still exists after rmtree: {target}")
        _chmod_tree_for_delete(target)
        time.sleep(float(sleep_sec) * float(attempt + 1))

    remaining_root = target if target.exists() else path
    remaining = _remaining_tree_entries(remaining_root)
    detail = str(last_error or "directory still exists").strip()
    if remaining:
        detail += "; remaining entries: " + ", ".join(remaining)
        if len(remaining) >= 20:
            detail += ", ..."
    raise RuntimeError(f"failed to remove instance root {path}: {detail}")


def _remove_empty_instance_parent(path: Path) -> bool:
    if path.name not in {"public_html", "docroot"}:
        return False
    parent = path.parent.resolve(strict=False)
    if parent.parent != _VAR_WWW.resolve(strict=False):
        return False
    try:
        parent.rmdir()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def _managed_image_db_user(db_name: str, db_user: str) -> str:
    clean_name = str(db_name or "").strip()
    clean_user = str(db_user or "").strip()
    if not clean_name.startswith("baza_"):
        return ""
    suffix = clean_name[len("baza_") :]
    expected = f"korisnik_{suffix}"
    if not suffix or len(expected) > 64 or not _DB_NAME_RE.match(expected):
        return ""
    if clean_user and clean_user != expected:
        return ""
    return expected


def _remove_dedicated_certificate(domain: str) -> tuple[bool, str]:
    cert_dir = _LETSENCRYPT_LIVE / domain
    renewal = _LETSENCRYPT_RENEWAL / f"{domain}.conf"
    if not cert_dir.exists() and not renewal.exists():
        return False, f"certificate already absent: {domain}"
    rc, out = _run(["certbot", "certificates", "--cert-name", domain], timeout_sec=60)
    if rc != 0:
        return False, f"certificate preserved; inspection failed for {domain}: {out}"
    match = re.search(r"(?im)^\s*Domains:\s*(.+?)\s*$", out)
    cert_domains = {value.strip().lower().rstrip(".") for value in (match.group(1).split() if match else [])}
    if cert_domains != {domain}:
        return False, f"certificate preserved; not dedicated to {domain}"
    rc, out = _run(
        ["certbot", "delete", "--cert-name", domain, "--non-interactive"],
        timeout_sec=120,
    )
    if rc != 0:
        return False, f"certificate cleanup failed for {domain}: {out}"
    safe_domain = re.sub(r"[^a-z0-9_.-]+", "_", domain.lower()).strip("._-") or "domain"
    credential = _LETSENCRYPT_MCD / f"dns-cloudflare-{safe_domain}.ini"
    try:
        if credential.exists():
            credential.unlink()
    except OSError as exc:
        return True, f"certificate removed for {domain}; credential cleanup failed: {exc}"
    return True, f"certificate removed for {domain}"


def delete_instance_artifacts(
    cfg: AgentConfig,
    *,
    root: str,
    domains: list[str] | None = None,
    db_name: str | None = None,
    delete_files: bool = False,
    delete_vhost: bool = False,
    delete_db: bool = False,
    yes: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not (delete_files or delete_vhost or delete_db):
        raise RuntimeError("select at least one deletion target")
    plan = build_delete_plan(
        cfg=cfg,
        root=root,
        domains=domains,
        db_name=db_name,
        delete_files=delete_files,
        delete_vhost=delete_vhost,
        delete_db=delete_db,
    )
    result: dict[str, Any] = {
        "status": "planned" if dry_run else "ok",
        "plan": _plan_public(plan),
        "deleted": {
            "files": False,
            "parent": False,
            "vhost": [],
            "db": False,
            "db_user": False,
            "local_mail": [],
            "certificates": [],
        },
        "warnings": [],
    }
    if dry_run:
        return result
    if not yes:
        raise RuntimeError("--yes is required")
    if os.geteuid() != 0:
        raise RuntimeError("must run as root")

    if plan.delete_vhost and plan.nginx_paths:
        rc, out = _run(["nginx", "-t"], timeout_sec=30)
        if rc != 0:
            raise RuntimeError("nginx -t failed before vhost removal: " + out)

    if plan.delete_vhost:
        from mcd_agent.local_mail import disable_local_mail

        for domain in plan.domains:
            try:
                mail_result = disable_local_mail(cfg, domain=domain)
            except Exception as exc:
                raise RuntimeError(f"failed to disable local mail for {domain}: {exc}") from exc
            if not bool(mail_result.get("already_disabled", False)):
                result["deleted"]["local_mail"].append(domain)

    if plan.delete_db:
        print(f"Dropping database {plan.db_name}")
        _mysql_exec(
            f"DROP DATABASE IF EXISTS {_quote_ident(plan.db_name)}",
            timeout_sec=300,
            host=plan.db_host,
            port=plan.db_port,
            user=plan.db_user,
            password=plan.db_password,
        )
        result["deleted"]["db"] = True
        managed_db_user = _managed_image_db_user(plan.db_name, plan.db_user)
        if managed_db_user:
            try:
                _mysql_exec(
                    f"DROP USER IF EXISTS {_quote_sql(managed_db_user)}@'localhost'",
                    timeout_sec=120,
                )
                result["deleted"]["db_user"] = True
            except Exception as exc:
                result["warnings"].append(f"managed database user cleanup failed: {exc}")

    if plan.delete_vhost:
        print("Deleting nginx vhost")
        if not plan.nginx_paths:
            result["warnings"].append("no matching nginx vhost files found")
        for path in plan.nginx_paths:
            try:
                changed, msg = _disable_nginx_vhost(path)
                result["warnings"].append(msg)
                if changed:
                    result["deleted"]["vhost"].append(str(path))
            except Exception as exc:
                raise RuntimeError(f"failed to remove nginx path {path}: {exc}") from exc
        for domain in plan.domains:
            changed, message = _remove_dedicated_certificate(domain)
            result["warnings"].append(message)
            if changed:
                result["deleted"]["certificates"].append(domain)
        if result["deleted"]["vhost"]:
            rc, out = _run(["nginx", "-t"], timeout_sec=30)
            if rc != 0:
                raise RuntimeError("nginx -t failed after vhost removal: " + out)
            _run(["systemctl", "reload", "nginx"], timeout_sec=30)

    if plan.delete_files:
        print(f"Deleting files {plan.root}")
        if plan.root.is_symlink():
            raise RuntimeError(f"refusing to delete symlink root: {plan.root}")
        if not plan.root.exists():
            result["warnings"].append(f"root already absent: {plan.root}")
            result["deleted"]["parent"] = _remove_empty_instance_parent(plan.root)
        else:
            _remove_instance_root(plan.root)
            result["deleted"]["files"] = True
            result["deleted"]["parent"] = _remove_empty_instance_parent(plan.root)

    try:
        from mcd_agent.inventory import InstanceInventory

        count = InstanceInventory(cfg.state_db_path).rescan(cfg)
        result["instances"] = count
        print(f"Rescan complete: {count} instances")
    except Exception as exc:
        result["warnings"].append(f"inventory rescan failed: {exc}")
    if result["warnings"]:
        result["status"] = "warning"
    return result
