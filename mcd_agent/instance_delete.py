from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import re
import shutil
import shlex
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import urlparse

from mcd_agent.config import AgentConfig
from mcd_agent.instance_identity import (
    INSTANCE_STATE_PROFILES,
    INSTANCE_STATE_SCHEMA,
    INSTANCE_STATE_VERSION,
    instance_state_path,
    read_instance_identity,
    read_instance_state,
    validate_instance_code,
    validate_mcc_instance_uid,
)
from mcd_agent.localphp import parse_local_php
from mcd_agent.models import DBConfig


_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_$-]{1,64}$")
_LOCAL_DB_HOSTS = {"", "localhost", "127.0.0.1", "::1"}
_NGINX_DIRS = (Path("/etc/nginx/sites-enabled"), Path("/etc/nginx/sites-available"), Path("/etc/nginx/conf.d"))
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
    instance_uid: str = ""
    instance_code: str = ""
    artifact_scope: bool = False
    identity_evidence: dict[str, Any] = field(default_factory=dict)
    state_evidence: dict[str, Any] = field(default_factory=dict)


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


def _nginx_directives(text: str, name: str) -> list[list[str]]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars="{};")
    lexer.whitespace_split = True
    statement: list[str] = []
    contexts: list[str] = []
    values: list[list[str]] = []
    try:
        for token in lexer:
            if token and all(char in "{};" for char in token):
                for char in token:
                    if char == "{":
                        contexts.append(statement[0] if statement else "")
                    elif char == "}" and contexts:
                        contexts.pop()
                    elif char == ";" and statement and statement[0] == name:
                        if name != "server_name" or not contexts or contexts[-1] == "server":
                            values.append(statement[1:])
                    statement = []
            else:
                statement.append(token)
    except ValueError as exc:
        raise RuntimeError("malformed nginx config; ownership unverified") from exc
    return values


def _nginx_server_names(text: str) -> set[str]:
    return {raw.lower().rstrip(".") for values in _nginx_directives(text, "server_name") for raw in values if raw}


def _nginx_matches_domain(path: Path, domain_set: set[str]) -> bool:
    if not domain_set:
        return False
    text = _read_nginx_text(path)
    if text:
        names = _nginx_server_names(text)
        if names:
            return bool(names.intersection(domain_set))
    filenames = {name for domain in domain_set for name in (domain, f"{domain}.conf")}
    return path.name.lower() in filenames or (path.is_symlink() and path.resolve(strict=False).name.lower() in filenames)


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
    result = [p for p in _dedupe_paths(wanted) if _safe_nginx_child(p)]
    for path in result:
        if _nginx_server_names(_read_nginx_text(path)) - domain_set:
            raise RuntimeError(f"shared nginx config must be preserved: {path}")
    # Capture aliases before removing their targets, including arbitrary names.
    return sorted(result, key=lambda p: not p.is_symlink())


def _disable_nginx_vhost(path: Path) -> tuple[bool, str]:
    if not (path.exists() or path.is_symlink()):
        return False, f"nginx vhost already absent: {path}"
    enabled_dir = _nginx_dir("sites-enabled").resolve(strict=False)
    try:
        parent = path.parent.resolve(strict=False)
    except Exception:
        raise RuntimeError(f"unsafe nginx path: {path}")
    if not _safe_nginx_child(path):
        raise RuntimeError(f"unsafe nginx path: {path}")
    if parent == enabled_dir:
        path.unlink()
        return True, f"removed enabled nginx config: {path}"
    path.unlink()
    label = "available" if path.parent.name == "sites-available" else path.parent.name
    return True, f"removed {label} nginx config: {path}"


def _certificate_referenced(nginx_dump: str, domain: str) -> bool:
    roots = (_LETSENCRYPT_LIVE / domain, _LETSENCRYPT_LIVE.parent / "archive" / domain)
    references = _nginx_directives(nginx_dump, "ssl_certificate") + _nginx_directives(nginx_dump, "ssl_certificate_key")
    for values in references:
        if len(values) != 1:
            return True
        value = values[0]
        if "$" in value:
            return True
        path = Path(value)
        if any(path.is_relative_to(root) or path.resolve(strict=False).is_relative_to(root.resolve(strict=False)) for root in roots):
            return True
    return False


def _remove_nginx_configs_safely(plan: InstanceDeletePlan, result: dict[str, Any]) -> set[str]:
    """Validate/reload scoped removal before any irreversible cert/data action."""
    from mcd_agent import nginx_baseline as baseline

    # Rescan immediately before mutation; the plan may predate new aliases.
    paths = _dedupe_paths([*_nginx_candidates(plan.root, plan.domains), *plan.nginx_paths])
    domains = set(plan.domains)
    for path in paths:
        if not _safe_nginx_child(path) or _nginx_server_names(_read_nginx_text(path)) - domains:
            raise RuntimeError(f"unsafe or shared nginx config: {path}")
    if not paths and not shutil.which("nginx"):
        result["warnings"].append("nginx unavailable; certificates preserved")
        return domains
    backup_dir = Path(tempfile.mkdtemp(prefix="mcd-delete-nginx-"))
    snapshots: dict[Path, Any] = {}
    changed_paths: list[str] = []
    reloaded = False
    try:
        for path in paths:
            baseline._snapshot(path, backup_dir, snapshots)
        for path in paths:
            changed, message = _disable_nginx_vhost(path)
            result["warnings"].append(message)
            if changed:
                changed_paths.append(str(path))
        rc, out = _run(["nginx", "-t"], timeout_sec=30)
        if rc != 0:
            raise RuntimeError("nginx -t failed after scoped vhost staging: " + out)
        rc, dump = _run(["nginx", "-T"], timeout_sec=30)
        if rc != 0:
            raise RuntimeError("nginx active-config inventory failed")
        if _nginx_server_names(dump) & domains:
            raise RuntimeError("target domain remains in active nginx configuration")
        retained = {domain for domain in domains if _certificate_referenced(dump, domain)}
        if changed_paths:
            rc, out = _run(["systemctl", "reload", "nginx"], timeout_sec=30)
            if rc != 0:
                raise RuntimeError("nginx reload failed after validated staging: " + out)
            reloaded = True
        result["deleted"]["vhost"].extend(changed_paths)
    except Exception as exc:
        baseline._restore_snapshots(snapshots)
        restored = True
        for snap in snapshots.values():
            try:
                if snap.kind == "symlink":
                    restored &= snap.path.is_symlink() and os.readlink(snap.path) == snap.target
                elif snap.kind == "file":
                    restored &= snap.path.is_file() and snap.path.read_bytes() == snap.backup.read_bytes()
                else:
                    restored &= not os.path.lexists(snap.path)
            except OSError:
                restored = False
        if not restored:
            raise RuntimeError(f"nginx rollback incomplete; retained snapshots in {backup_dir}") from exc
        if reloaded:
            rc, _ = _run(["nginx", "-t"], timeout_sec=30)
            if rc == 0:
                _run(["systemctl", "reload", "nginx"], timeout_sec=30)
        # Keep the root-only durable snapshots for recovery on failed rollback.
        raise
    shutil.rmtree(backup_dir)
    return retained


def _inventory_db_for_root(cfg: AgentConfig | None, root: Path) -> DBConfig | None:
    state_db_path = str(getattr(cfg, "state_db_path", "") or "").strip()
    if not state_db_path:
        return None
    try:
        from mcd_agent.inventory import InstanceInventory

        inventory = InstanceInventory(state_db_path)
        for inst in inventory.list_all_instances():
            if Path(inst.root).resolve(strict=False) == root and inst.db is not None:
                return inst.db
    except Exception:
        return None
    return None


def _inventory_identity_records(cfg: AgentConfig | None) -> list[dict[str, Any]]:
    state_db_path = str(getattr(cfg, "state_db_path", "") or "").strip()
    if not state_db_path:
        return []
    try:
        from mcd_agent.inventory import InstanceInventory

        inventory = InstanceInventory(state_db_path)
        records: list[dict[str, Any]] = []
        for inst in inventory.list_all_instances():
            try:
                local_state = read_instance_state(inst.root) or {}
            except ValueError:
                local_state = {}
            domains = [str(value).strip().lower().rstrip(".") for value in (getattr(inst, "domains", []) or []) if str(value).strip()]
            primary = str(local_state.get("primary_domain") or getattr(inst, "primary_domain", "") or "").strip().lower().rstrip(".")
            if primary and primary not in domains:
                domains.insert(0, primary)
            records.append(
                {
                    "source": "inventory",
                    "root": str(Path(getattr(inst, "root", "")).resolve(strict=False)),
                    "instance_uid": str(local_state.get("instance_uid") or getattr(inst, "instance_uid", "") or "").strip(),
                    "instance_code": str(local_state.get("instance_code") or getattr(inst, "instance_code", "") or "").strip(),
                    "primary_domain": primary,
                    "domains": domains,
                }
            )
        return records
    except Exception:
        return []


def _bare_identity_uid(value: object) -> str:
    return str(value or "").strip().split("@", 1)[0]


def _instance_state_evidence(
    *,
    root: Path,
    requested_state: str | None,
    instance_uid: str | None,
    instance_code: str | None,
    requested_domains: list[str],
) -> dict[str, Any]:
    """Resolve MCC state first, using the local state file only as fallback evidence."""
    local_path = instance_state_path(root)
    try:
        local_state = read_instance_state(root)
    except ValueError as exc:
        raise RuntimeError(f"instance state evidence invalid: {exc}") from exc

    supplied_state = str(requested_state or "").strip().lower()
    if supplied_state and supplied_state not in INSTANCE_STATE_PROFILES:
        raise RuntimeError(f"instance state profile is unknown: {supplied_state}")

    if supplied_state and local_state is not None:
        if str(local_state.get("profile") or "") != supplied_state:
            raise RuntimeError("instance state conflict between MCC registry and local .mcd/state evidence")
        for key, expected in (
            ("instance_uid", str(instance_uid or "").strip()),
            ("instance_code", str(instance_code or "").strip()),
        ):
            observed = str(local_state.get(key) or "").strip()
            if observed and expected and observed != expected:
                raise RuntimeError(f"instance state conflict for {key}")
        requested_domains_clean = {_clean_domain(value) for value in requested_domains if str(value or "").strip()}
        observed_domain = str(local_state.get("primary_domain") or "").strip().lower().rstrip(".")
        if observed_domain and requested_domains_clean and observed_domain not in requested_domains_clean:
            raise RuntimeError("instance state conflict for primary domain")

    effective_uid = str(instance_uid or (local_state or {}).get("instance_uid") or "").strip()
    effective_code = str(instance_code or (local_state or {}).get("instance_code") or "").strip()

    if supplied_state:
        selected = {
            "schema": INSTANCE_STATE_SCHEMA,
            "version": INSTANCE_STATE_VERSION,
            "profile": supplied_state,
            "source": "mcc_registry",
            "path": str(local_path),
            "local_evidence": local_state is not None,
            "ownership_confirmed": True,
            "cluster_id": str((local_state or {}).get("cluster_id") or ""),
        }
    elif local_state is not None:
        selected = {
            **local_state,
            "source": "local_fallback",
            "path": str(local_path),
            "local_evidence": True,
        }
    else:
        if instance_uid or instance_code:
            raise RuntimeError("instance state evidence is unavailable for exact artifact deletion")
        return {
            "schema": INSTANCE_STATE_SCHEMA,
            "version": INSTANCE_STATE_VERSION,
            "status": "not_requested",
            "source": "none",
            "profile": "",
            "path": str(local_path),
        }

    profile = str(selected.get("profile") or "")
    if profile == "cluster":
        if not str(selected.get("cluster_id") or "").strip() or not bool(selected.get("ownership_confirmed")):
            raise RuntimeError("cluster instance state is incomplete")
        raise RuntimeError("cluster instance state requires separate routing; destructive deletion refused")
    if profile != "artifact":
        raise RuntimeError(f"destructive deletion requires instance state profile artifact, got {profile}")
    if not bool(selected.get("ownership_confirmed")):
        raise RuntimeError("instance state ownership confirmation is missing")
    if not effective_uid:
        raise RuntimeError("artifact state requires a persisted instance_uid")
    if not effective_code and (local_state is None or str(local_state.get("instance_code") or "").strip()):
        raise RuntimeError("artifact state requires instance_code unless persisted state is legacy UID-only")

    return {
        **selected,
        "status": "matched",
        "instance_uid": effective_uid,
        "instance_code": effective_code,
        "legacy_uid_only": not bool(effective_code),
    }


def _artifact_identity_evidence(
    *,
    cfg: AgentConfig | None,
    root: Path,
    requested_domains: list[str],
    site_domain: str,
    instance_uid: str | None,
    instance_code: str | None,
    force_exact: bool = False,
) -> dict[str, Any]:
    supplied_uid = str(instance_uid or "").strip()
    supplied_code = str(instance_code or "").strip()
    if not supplied_uid and not supplied_code and not force_exact:
        return {"scope": "ordinary", "status": "not_requested"}
    try:
        local_state = read_instance_state(root)
    except ValueError as exc:
        raise RuntimeError(f"instance state evidence invalid: {exc}") from exc
    effective_uid = supplied_uid or str((local_state or {}).get("instance_uid") or "").strip()
    effective_code = supplied_code or str((local_state or {}).get("instance_code") or "").strip()
    legacy_uid_only = (
        not effective_code
        and local_state is not None
        and str(local_state.get("profile") or "").strip().lower() == "artifact"
        and bool(str(local_state.get("instance_uid") or "").strip())
    )
    if not effective_uid or (not effective_code and not legacy_uid_only):
        raise RuntimeError("exact artifact deletion requires a persisted UID and code, except for legacy UID-only artifact state")
    try:
        expected_uid = validate_mcc_instance_uid(effective_uid)
        expected_code = validate_instance_code(effective_code) if effective_code else ""
    except ValueError as exc:
        raise RuntimeError(f"invalid exact artifact identity: {exc}") from exc

    explicit_domains = [_clean_domain(value) for value in requested_domains if str(value or "").strip()]
    explicit_domains = list(dict.fromkeys(value for value in explicit_domains if value))
    if not explicit_domains and local_state is not None:
        persisted_domain = _clean_domain(str(local_state.get("primary_domain") or ""))
        if persisted_domain:
            explicit_domains = [persisted_domain]
    if len(explicit_domains) != 1:
        raise RuntimeError("exact artifact deletion requires one explicit domain")
    expected_domain = explicit_domains[0]
    if site_domain and site_domain != expected_domain:
        raise RuntimeError(
            f"exact artifact identity domain mismatch: local.php={site_domain} requested={expected_domain}"
        )

    target_root = str(root.resolve(strict=False))
    marker_path = root / ".mcd-instance-identity.json"
    marker = read_instance_identity(root)
    if marker_path.exists() and marker is None:
        raise RuntimeError("exact artifact identity marker is invalid")
    records = _inventory_identity_records(cfg)
    root_records = [record for record in records if str(record.get("root") or "") == target_root]
    if len(root_records) > 1:
        raise RuntimeError("exact artifact identity is ambiguous: multiple inventory rows share the root")

    duplicate_uid_roots = sorted(
        {
            str(record.get("root") or "")
            for record in records
            if _bare_identity_uid(record.get("instance_uid")) == expected_uid
            and str(record.get("root") or "") != target_root
        }
    )
    if duplicate_uid_roots:
        raise RuntimeError(
            "exact artifact identity is ambiguous: UID is registered on another local root "
            + ", ".join(duplicate_uid_roots)
        )

    candidates: list[dict[str, Any]] = []
    if marker is not None:
        candidates.append({"source": "marker", **marker})
    if local_state is not None:
        candidates.append({"source": "local_state", **local_state})
    candidates.extend(root_records)
    if not candidates:
        raise RuntimeError("exact artifact identity evidence is unavailable")

    sources: list[str] = []
    for candidate in candidates:
        candidate_uid = _bare_identity_uid(candidate.get("instance_uid"))
        candidate_code = str(candidate.get("instance_code") or "").strip()
        candidate_domain = str(candidate.get("primary_domain") or "").strip().lower().rstrip(".")
        candidate_domains = {
            str(value).strip().lower().rstrip(".")
            for value in (candidate.get("domains") or [])
            if str(value).strip()
        }
        if candidate_uid != expected_uid:
            raise RuntimeError("exact artifact identity evidence does not match requested UID")
        if expected_code and candidate_code != expected_code:
            raise RuntimeError("exact artifact identity evidence does not match requested UID/code")
        if not expected_code and candidate_code:
            raise RuntimeError("legacy UID-only artifact has coded identity evidence")
        if candidate_domain and candidate_domain != expected_domain:
            raise RuntimeError("exact artifact identity evidence does not match requested domain")
        if candidate_domains and expected_domain not in candidate_domains:
            raise RuntimeError("exact artifact identity domain is not registered for the requested root")
        sources.append(str(candidate.get("source") or "unknown"))

    return {
        "scope": "exact-artifact",
        "status": "matched",
        "instance_uid": expected_uid,
        "instance_code": expected_code,
        "legacy_uid_only": legacy_uid_only,
        "domain": expected_domain,
        "root": target_root,
        "sources": list(dict.fromkeys(sources)),
    }


def build_delete_plan(
    *,
    cfg: AgentConfig | None = None,
    root: str,
    domains: list[str] | None = None,
    db_name: str | None = None,
    delete_files: bool = False,
    delete_vhost: bool = False,
    delete_db: bool = False,
    instance_uid: str | None = None,
    instance_code: str | None = None,
    instance_state: str | None = None,
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

    state_evidence = _instance_state_evidence(
        root=target_root,
        requested_state=instance_state,
        instance_uid=instance_uid,
        instance_code=instance_code,
        requested_domains=list(domains or []),
    )
    identity_evidence = _artifact_identity_evidence(
        cfg=cfg,
        root=target_root,
        requested_domains=list(domains or []),
        site_domain=site_domain,
        instance_uid=instance_uid,
        instance_code=instance_code,
        force_exact=state_evidence.get("profile") == "artifact",
    )

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
        instance_uid=str(identity_evidence.get("instance_uid") or instance_uid or "").strip(),
        instance_code=str(identity_evidence.get("instance_code") or instance_code or "").strip(),
        artifact_scope=identity_evidence.get("scope") == "exact-artifact",
        identity_evidence=identity_evidence,
        state_evidence=state_evidence,
    )


def _plan_public(plan: InstanceDeletePlan) -> dict[str, Any]:
    managed_db_user = _managed_image_db_user(plan.db_name, plan.db_user) if plan.delete_db else ""
    if not plan.delete_db:
        db_identity_status = "not_requested"
    elif managed_db_user:
        db_identity_status = "matched"
    else:
        db_identity_status = "invalid"
    return {
        "root": str(plan.root),
        "local_php": str(plan.local_php) if plan.local_php else "",
        "domains": list(plan.domains),
        "nginx_paths": [str(p) for p in plan.nginx_paths],
        "db_name": plan.db_name,
        "db_host": plan.db_host or "localhost",
        "db_port": plan.db_port or "3306",
        "db_user": plan.db_user,
        "db_identity": {
            "contract": "mcd-managed-instance-db-v1",
            "status": db_identity_status,
            "managed_db_user": managed_db_user,
        },
        "artifact_identity": dict(plan.identity_evidence),
        "artifact_scope": plan.artifact_scope,
        "instance_state": dict(plan.state_evidence),
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
    """Return the user only for an exact MCD-managed database/user pair.

    MCD has two canonical instance layouts in the field: the legacy image
    layout (``baza_X``/``korisnik_X``) and the MCC identity layout
    (``db_<instance-code>``/``usr_<instance-code>``).  A database name alone
    is never enough to authorize user deletion: an empty, mismatched, or
    shared-looking user fails closed.
    """
    clean_name = str(db_name or "").strip()
    clean_user = str(db_user or "").strip()
    expected = ""
    if clean_name.startswith("baza_"):
        suffix = clean_name[len("baza_") :]
        if not suffix:
            return ""
        expected = f"korisnik_{suffix}"
    elif clean_name.startswith("db_"):
        code = clean_name[len("db_") :]
        try:
            code = validate_instance_code(code)
        except ValueError:
            return ""
        expected = f"usr_{code}"
    else:
        return ""
    if not expected or len(expected) > 64 or not _DB_NAME_RE.match(expected):
        return ""
    if clean_user != expected:
        return ""
    return expected


def _managed_db_user_hosts(managed_user: str) -> list[str]:
    """Enumerate every host row for one exact managed account.

    The query is deliberately scoped to the canonical managed username. Any
    malformed or differently named row is treated as an ownership ambiguity,
    never as permission to broaden cleanup.
    """
    expected_user = str(managed_user or "").strip()
    if not expected_user:
        raise RuntimeError("managed database user is empty")
    output = _mysql_exec(
        "SELECT User, Host FROM mysql.user "
        f"WHERE User = {_quote_sql(expected_user)} ORDER BY Host",
        timeout_sec=120,
    )
    hosts: set[str] = set()
    for line in str(output or "").splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 2:
            raise RuntimeError("managed database user inventory row is malformed")
        observed_user, host = (value.strip() for value in fields)
        if observed_user != expected_user:
            raise RuntimeError("managed database user inventory contains an unrelated account")
        if not host or any(char in host for char in "\x00\r\n\t") or len(host) > 255:
            raise RuntimeError("managed database user inventory contains an invalid host")
        hosts.add(host)
    return sorted(hosts)


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
    instance_uid: str | None = None,
    instance_code: str | None = None,
    instance_state: str | None = None,
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
        instance_uid=instance_uid,
        instance_code=instance_code,
        instance_state=instance_state,
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
            "db_user_hosts": [],
            "db_user_remaining_hosts": [],
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

    managed_db_user = _managed_image_db_user(plan.db_name, plan.db_user) if plan.delete_db else ""
    if plan.delete_db and not managed_db_user:
        raise RuntimeError(
            "refusing database deletion: db_name/db_user must be an exact managed pair "
            "(baza_X/korisnik_X or db_<5-char-code>/usr_<same-code>)"
        )
    managed_db_user_hosts: list[str] = []
    if plan.delete_db:
        try:
            managed_db_user_hosts = _managed_db_user_hosts(managed_db_user)
        except Exception as exc:
            raise RuntimeError(
                f"refusing database deletion: managed user ownership inventory failed: {exc}"
            ) from exc
        result["deleted"]["db_user_hosts"] = list(managed_db_user_hosts)

    retained_certificates = _remove_nginx_configs_safely(plan, result) if plan.delete_vhost else set()

    if plan.delete_vhost and not plan.artifact_scope:
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
        for host in managed_db_user_hosts:
            try:
                _mysql_exec(
                    f"DROP USER IF EXISTS {_quote_sql(managed_db_user)}@{_quote_sql(host)}",
                    timeout_sec=120,
                )
            except Exception as exc:
                result["warnings"].append(f"managed database user cleanup failed for {host}: {exc}")
        try:
            remaining_hosts = _managed_db_user_hosts(managed_db_user)
            result["deleted"]["db_user_remaining_hosts"] = list(remaining_hosts)
            if remaining_hosts:
                result["warnings"].append(
                    "managed database user accounts remain: " + ", ".join(remaining_hosts)
                )
            else:
                result["deleted"]["db_user"] = True
        except Exception as exc:
            result["warnings"].append(f"managed database user verification failed: {exc}")

    if plan.delete_vhost:
        print("Deleting nginx vhost")
        if not plan.nginx_paths:
            result["warnings"].append("no matching nginx vhost files found")
        for domain in plan.domains:
            if domain not in retained_certificates:
                rc, dump = _run(["nginx", "-T"], timeout_sec=30)
                if rc != 0:
                    raise RuntimeError("nginx inventory failed before certificate cleanup")
                if _certificate_referenced(dump, domain):
                    retained_certificates.add(domain)
            if domain in retained_certificates:
                result["warnings"].append(f"certificate preserved; still referenced by nginx: {domain}")
                continue
            changed, message = _remove_dedicated_certificate(domain)
            result["warnings"].append(message)
            if changed:
                result["deleted"]["certificates"].append(domain)

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
