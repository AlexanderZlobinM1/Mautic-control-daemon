from __future__ import annotations

from datetime import datetime, timezone
import gzip
import hashlib
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
from mcd_agent.mautic_image_install import (
    ImageInstallPlan,
    _load_pem_certificate_identity,
    _mysql_admin_base,
    _mysql_exec,
    _quote_ident,
    _quote_sql,
    _run_certbot,
    _safe_runtime_detail,
    _verify_image_https,
)
from mcd_agent.mysql_dump_compat import iter_mysql_import_sql
from mcd_agent.mautic_version_cache import read_mautic_version_evidence_read_only
from mcd_agent.form_embed import render_form_embed_location
from mcd_agent.models import DBConfig
from mcd_agent.models import MauticInstall
from mcd_agent.maintenance_mode import (
    collect_maintenance_state,
    restore_cron_service_if_needed,
    stop_cron_service,
    stop_running_tasks_for_maintenance,
)
from mcd_agent.nginx_baseline import (
    _nginx_supports_http2_directive,
    ensure_mautic_public_app_asset_locations,
    ensure_nginx_baseline,
    normalize_legacy_http2_listen,
)
from mcd_agent.nginx_templates import render_nginx_template
from mcd_agent.runtime_adapters import run_runtime_adapter, runtime_adapter_path
from mcd_agent.runtime_descriptor import descriptor_for_root
from mcd_agent.runtime_matrix import build_runtime_profile
from mcd_agent.source_operation import stable_source_operation
from mcd_agent.instance_uid import instance_uid_matches
from mcd_agent.instance_identity import (
    INSTANCE_IDENTITY_MARKER,
    identity_readback,
    instance_path_names,
    read_instance_identity,
    read_instance_state,
    validate_instance_code,
    validate_mcc_instance_uid,
    write_instance_identity,
    write_instance_state,
)


_MIN_TARGET_HEADROOM_BYTES = 5 * 1024 * 1024 * 1024
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_DB_IDENT_RE = re.compile(r"^[A-Za-z0-9_$-]{1,64}$")
_MYSQL_DATE_FORMAT_GENERATED_RE = re.compile(
    r"GENERATED\s+ALWAYS\s+AS\s*\(\s*date_format\s*\(\s*`(?P<column>[^`]+)`\s*,\s*(?:_[A-Za-z0-9]+)?'(?P<fmt>[^']+)'\s*\)\s*\)",
    re.IGNORECASE,
)
_DB_STREAM_MARKER_RE = re.compile(
    rb"^-- MCD-DB-STREAM-V1 (?P<phase>BEGIN|COMPLETE) (?P<stream_id>[0-9a-f]{32})\n$"
)
_DB_STREAM_COPY_BUFFER_BYTES = 1024 * 1024
NGINX_SITES_AVAILABLE = Path("/etc/nginx/sites-available")
NGINX_SITES_ENABLED = Path("/etc/nginx/sites-enabled")
LETSENCRYPT_ROOT = Path("/etc/letsencrypt")
MIGRATION_ROOT = Path("/var/www")
MIGRATION_TARGET_MARKER = ".mcd-instance-migration-target.json"
MIGRATION_SOURCE_STATE_ROOT = Path("/opt/mcd/var/instance-migration-source")
MIGRATION_SOURCE_MAINTENANCE_ROOT = Path("/opt/mcd/var/instance-migration-maintenance")
MIGRATION_SOURCE_STATE_SCHEMA = "mcd-instance-migration-source-maintenance-v1"


def _mcc_wire_host_name(config: AgentConfig) -> str:
    from mcd_agent.host_identity import resolve_agent_identity
    try:
        identity = resolve_agent_identity(config)
    except Exception:
        return str(getattr(config, "mcc_host_name", "") or "").strip()
    return str(identity.get("effective_mcc_host_name") or "").strip()


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


def _path_is_below(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _canonical_migration_target(domains: list[str], instance_code: str | None = None) -> Path | None:
    if not domains:
        return None
    if instance_code:
        code = validate_instance_code(instance_code)
        return MIGRATION_ROOT / f"{domains[0]}_{code}" / "public_html"
    return MIGRATION_ROOT / domains[0] / "public_html"


def _migration_target_marker(target: Path) -> Path:
    return target / MIGRATION_TARGET_MARKER


def _migration_target_owned(target: Path) -> bool:
    marker = _migration_target_marker(target)
    if not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(data, dict)
        and data.get("schema") == "mcd-instance-migration-target-v1"
        and data.get("state") in {"in_progress", "failed"}
    )


def _utc_archive_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{stamp}-{path.name}")
    index = 2
    while candidate.exists():
        candidate = path.with_name(f"{stamp}-{path.name}-{index}")
        index += 1
    return candidate


def _archive_migration_target(target: Path) -> Path:
    if not _migration_target_owned(target):
        raise RuntimeError(f"target is not an unfinished MCD migration target: {target}")
    archive = _utc_archive_path(target)
    target.rename(archive)
    return archive


def _remove_exact_migration_target(
    target: Path,
    *,
    domains: list[str],
    instance_uid: str,
    instance_code: str,
) -> bool:
    uid = validate_mcc_instance_uid(instance_uid)
    code = validate_instance_code(instance_code)
    canonical = _canonical_migration_target(domains, code)
    if canonical is None or target.resolve(strict=False) != canonical.resolve(strict=False):
        raise RuntimeError("target cleanup path does not match the canonical MCC identity path")
    if target.is_symlink():
        raise RuntimeError(f"target cleanup root is not a directory: {target}")
    if not target.exists():
        return False
    if not target.is_dir():
        raise RuntimeError(f"target cleanup root is not a directory: {target}")
    marker_path = _migration_target_marker(target)
    if marker_path.exists():
        marker = _read_migration_target_marker(target)
        marker_domains = [_clean_domain(str(value or "")) for value in list(marker.get("domains") or [])]
        if marker.get("state") not in {"in_progress", "failed"}:
            raise RuntimeError("target cleanup migration marker is not transient")
        if str(marker.get("instance_uid") or "") != uid or str(marker.get("instance_code") or "") != code:
            raise RuntimeError("target cleanup migration marker identity does not match request")
        if marker_domains != domains:
            raise RuntimeError("target cleanup migration marker domains do not match request")
    else:
        identity = read_instance_identity(target)
        if identity is None:
            raise RuntimeError("target cleanup durable instance identity is missing")
        if identity.get("instance_uid") != uid or identity.get("instance_code") != code:
            raise RuntimeError("target cleanup durable instance identity does not match request")
        if identity.get("primary_domain") != domains[0]:
            raise RuntimeError("target cleanup durable instance domain does not match request")
    _assert_safe_target_wipe_path(target)
    shutil.rmtree(target)
    return True


def _write_migration_target_marker(
    target: Path,
    *,
    domains: list[str],
    source_root: str = "",
    instance_uid: str | None = None,
    instance_code: str | None = None,
) -> Path:
    identity: dict[str, str] = {}
    if instance_uid or instance_code:
        if not instance_uid or not instance_code:
            raise RuntimeError("instance_uid and instance_code must be supplied together by MCC")
        identity = {
            "instance_uid": validate_mcc_instance_uid(instance_uid),
            "instance_code": validate_instance_code(instance_code),
        }
    marker = _migration_target_marker(target)
    marker.write_text(
        json.dumps(
            {
                "schema": "mcd-instance-migration-target-v1",
                "state": "in_progress",
                "domains": domains,
                "source_root": source_root,
                "created_at_utc": _utc_now(),
                **identity,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return marker


def _mark_migration_target_failed(target: Path, error: str) -> None:
    marker = _migration_target_marker(target)
    if not marker.is_file():
        return
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict) or data.get("schema") != "mcd-instance-migration-target-v1":
        return
    data["state"] = "failed"
    data["error"] = str(error)[-2000:]
    data["failed_at_utc"] = _utc_now()
    try:
        marker.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return


def _read_migration_target_marker(target: Path) -> dict[str, Any]:
    marker = _migration_target_marker(target)
    if not marker.is_file():
        raise RuntimeError(f"migration target marker is missing: {target}")
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"migration target marker is invalid: {target}") from exc
    if not isinstance(data, dict) or data.get("schema") != "mcd-instance-migration-target-v1":
        raise RuntimeError(f"migration target marker schema is invalid: {target}")
    return data


def _mark_migration_target_prepared(target: Path) -> dict[str, Any]:
    data = _read_migration_target_marker(target)
    if str(data.get("state") or "") != "in_progress":
        raise RuntimeError(f"migration target is not in progress: {target}")
    data["phase"] = "target_prepared"
    data["prepared_at_utc"] = _utc_now()
    marker = _migration_target_marker(target)
    marker.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def _prepare_migration_target(
    target: Path,
    *,
    domains: list[str],
    claim: bool,
    source_root: str = "",
    instance_uid: str | None = None,
    instance_code: str | None = None,
) -> Path | None:
    """Validate a new target and claim only MCD-owned unfinished residue."""
    managed = _path_is_below(target, MIGRATION_ROOT)
    if bool(instance_uid) != bool(instance_code):
        raise RuntimeError("instance_uid and instance_code must be supplied together by MCC")
    if instance_uid:
        validate_mcc_instance_uid(instance_uid)
    if instance_code:
        validate_instance_code(instance_code)
    canonical = _canonical_migration_target(domains, instance_code)
    canonical_label = "canonical MCC identity path" if instance_code else "canonical full-FQDN path"
    if managed and canonical is None:
        raise RuntimeError("new /var/www migration target requires a domain")
    if managed and not target.exists() and canonical is not None and target != canonical:
        raise RuntimeError(
            f"new migration target must use {canonical_label} {canonical}, got {target}"
        )

    archived: Path | None = None
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise RuntimeError(f"target root is occupied by a non-directory: {target}")
        if _migration_target_owned(target):
            archived = _archive_migration_target(target)
        elif any(target.iterdir()):
            raise RuntimeError(f"target root already exists and is not empty: {target}")
        else:
            if managed and canonical is not None and target != canonical:
                raise RuntimeError(
                    f"new migration target must use {canonical_label} {canonical}, got {target}"
                )
            target.rmdir()

    if claim:
        target.mkdir(parents=True, exist_ok=True)
        if managed:
            _write_migration_target_marker(
                target,
                domains=domains,
                source_root=source_root,
                instance_uid=instance_uid,
                instance_code=instance_code,
            )
    return archived


def _finish_migration_target(target: Path) -> None:
    marker = _migration_target_marker(target)
    if marker.is_file():
        marker.unlink()


def _atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.chmod(mode)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _source_migration_state_path(instance_uid: str) -> Path:
    uid = validate_mcc_instance_uid(instance_uid)
    return MIGRATION_SOURCE_STATE_ROOT / f"{uid}.json"


def _write_source_migration_state(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=True, indent=2) + "\n")


def _read_source_migration_state(instance_uid: str) -> tuple[Path, dict[str, Any]]:
    path = _source_migration_state_path(instance_uid)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError("source migration maintenance state is missing") from exc
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("source migration maintenance state is invalid") from exc
    if not isinstance(raw, dict) or raw.get("schema") != MIGRATION_SOURCE_STATE_SCHEMA:
        raise RuntimeError("source migration maintenance state schema is invalid")
    return path, raw


def _migration_source_identity(
    config: AgentConfig,
    *,
    selector: str | None,
    domains: list[str],
    instance_uid: str,
    instance_code: str,
) -> tuple[MauticInstall, Path, dict[str, str], dict[str, Any]]:
    uid = validate_mcc_instance_uid(instance_uid)
    code = validate_instance_code(instance_code)
    if not domains:
        raise RuntimeError("source migration maintenance requires at least one domain")
    inv = InstanceInventory(config.state_db_path)
    ensure_seeded(inv, config)
    inst = _select_instance(inv.list_instances(), selector, host_name=_mcc_wire_host_name(config))
    root = Path(inst.root).resolve()
    if not root.is_dir():
        raise RuntimeError(f"source root is not a directory: {root}")
    selector_token = str(selector or "").strip()
    inventory_identity = {
        "instance_uid": str(inst.instance_uid or ""),
        "instance_code": str(inst.instance_code or ""),
        "primary_domain": str(inst.primary_domain or "").strip().lower().rstrip("."),
        "root": str(root),
    }
    observed = read_instance_identity(root)
    if observed is None:
        marker_path = root / INSTANCE_IDENTITY_MARKER
        if marker_path.exists():
            raise RuntimeError("source instance identity marker is present but invalid")
        if not selector_token or not Path(selector_token).is_absolute() or Path(selector_token).resolve() != root:
            raise RuntimeError("missing source identity marker can be adopted only for the exact absolute source root")
        known_domains = {
            str(value or "").strip().lower().rstrip(".")
            for value in [inst.primary_domain, inst.name, *(inst.domains or [])]
            if str(value or "").strip()
        }
        if domains[0] not in known_domains:
            raise RuntimeError("source inventory primary domain does not match migration request")
        if str(inst.instance_code or "").strip():
            if validate_instance_code(inst.instance_code) != code:
                raise RuntimeError("source inventory instance code does not match migration request")
            if not instance_uid_matches(uid, str(inst.instance_uid or ""), _mcc_wire_host_name(config)):
                raise RuntimeError("source inventory MCC identity does not match migration request")
        try:
            local_state = read_instance_state(root)
        except ValueError as exc:
            raise RuntimeError("source instance state evidence is invalid") from exc
        if local_state is not None:
            state_uid = str(local_state.get("instance_uid") or "").strip()
            state_code = str(local_state.get("instance_code") or "").strip()
            state_domain = str(local_state.get("primary_domain") or "").strip().lower().rstrip(".")
            if state_uid and state_uid != uid:
                raise RuntimeError("source instance state UID does not match migration request")
            if state_code and state_code != code:
                raise RuntimeError("source instance state code does not match migration request")
            if state_domain and state_domain not in domains:
                raise RuntimeError("source instance state domain does not match migration request")
        write_instance_identity(
            root,
            instance_uid=uid,
            instance_code=code,
            primary_domain=domains[0],
        )
        observed = read_instance_identity(root)
        if observed is None:
            raise RuntimeError("source instance identity marker materialization failed")
        adoption = {
            "materialized_from_mcc": True,
            "reason": "legacy_source_marker_missing",
            "inventory_identity": inventory_identity,
        }
    else:
        adoption = {
            "materialized_from_mcc": False,
            "reason": "existing_marker_verified",
            "inventory_identity": inventory_identity,
        }
    if observed.get("instance_uid") != uid or observed.get("instance_code") != code:
        raise RuntimeError("source instance identity does not match migration request")
    primary = str(observed.get("primary_domain") or "").strip().lower().rstrip(".")
    if primary and primary not in domains:
        raise RuntimeError("source primary domain does not match migration request")
    return inst, root, observed, adoption


def _nginx_directive_values(content: str, directive: str) -> list[str]:
    values: list[str] = []
    pattern = re.compile(rf"(?m)^\s*{re.escape(directive)}\s+([^;]+);\s*(?:#.*)?$")
    for match in pattern.finditer(content):
        value = str(match.group(1) or "").strip().strip("\"'")
        if value:
            values.append(value)
    return values


def _find_source_vhost(root: Path, domains: list[str]) -> tuple[Path, str, Path, str]:
    if not NGINX_SITES_ENABLED.is_dir():
        raise RuntimeError(f"nginx sites-enabled directory is missing: {NGINX_SITES_ENABLED}")
    expected_roots = {root.resolve(strict=False), _nginx_web_root(root).resolve(strict=False)}
    requested = set(domains)
    matches: list[tuple[Path, str, Path, str]] = []
    for link in sorted(NGINX_SITES_ENABLED.iterdir()):
        if not link.is_symlink():
            continue
        raw_target = os.readlink(link)
        config_path = (link.parent / raw_target).resolve(strict=False)
        if not config_path.is_file():
            continue
        try:
            content = config_path.read_text(encoding="utf-8", errors="strict")
        except OSError:
            continue
        names: set[str] = set()
        for value in _nginx_directive_values(content, "server_name"):
            names.update(token.lower().rstrip(".") for token in value.split() if token and token != "_")
        if not requested.intersection(names):
            continue
        configured_roots: set[Path] = set()
        for value in _nginx_directive_values(content, "root"):
            if "$" in value:
                continue
            configured_roots.add(Path(value).resolve(strict=False))
        if not expected_roots.intersection(configured_roots):
            continue
        matches.append((link, raw_target, config_path, content))
    if not matches:
        raise RuntimeError("active source nginx symlink was not found for the selected root and domains")
    if len(matches) != 1:
        names = ", ".join(str(item[0]) for item in matches)
        raise RuntimeError(f"source nginx vhost selection is ambiguous: {names}")
    return matches[0]


def _maintenance_page() -> str:
    return """<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<meta http-equiv=\"refresh\" content=\"30\"><title>Sales Snap maintenance</title>
<style>body{margin:0;background:#f4f7fb;color:#172033;font:16px/1.5 system-ui,sans-serif;display:grid;min-height:100vh;place-items:center}.card{max-width:620px;margin:24px;padding:38px;border-radius:20px;background:#fff;box-shadow:0 18px 60px #24324a20}.brand{font-weight:800;color:#3157d5;letter-spacing:.04em}.muted{color:#60708c}</style></head>
<body><main class=\"card\"><div class=\"brand\">SALES SNAP</div><h1>Service migration in progress</h1><p>The service is temporarily unavailable while it is moved safely.</p><p class=\"muted\">This page refreshes automatically. Please try again shortly.</p></main></body></html>
"""


def _render_source_maintenance_vhost(*, original: str, domains: list[str], page_root: Path) -> str:
    certs = _nginx_directive_values(original, "ssl_certificate")
    keys = _nginx_directive_values(original, "ssl_certificate_key")
    has_tls = bool(re.search(r"(?m)^\s*listen\s+[^;]*\b443\b[^;]*;", original))
    if has_tls and (not certs or not keys):
        raise RuntimeError("source TLS vhost does not expose certificate paths in its preserved config")
    for value in [*certs[:1], *keys[:1]]:
        if not value.startswith("/") or any(ch in value for ch in "\r\n;"):
            raise RuntimeError("source TLS certificate path is unsafe")
    names = " ".join(domains)
    page_dir = str(page_root)

    def block(listen: str, tls: bool) -> str:
        tls_lines = ""
        if tls:
            tls_lines = f"    ssl_certificate {certs[0]};\n    ssl_certificate_key {keys[0]};\n"
        return (
            "server {\n"
            f"    listen {listen};\n"
            f"    server_name {names};\n"
            f"{tls_lines}"
            "    add_header Cache-Control \"no-store\" always;\n"
            "    error_page 503 =503 /mcd-migration-maintenance.html;\n"
            "    location = /mcd-migration-maintenance.html {\n"
            f"        root {page_dir};\n"
            "        internal;\n"
            "    }\n"
            "    location / { return 503; }\n"
            "}\n"
        )

    rendered = "# Managed by MCD instance migration; static 503 only.\n" + block("80", False)
    if has_tls:
        rendered += "\n" + block("443 ssl", True)
    prohibited = ("fastcgi", "proxy_pass", "uwsgi", "scgi_pass", "php")
    if any(token in rendered.lower() for token in prohibited):
        raise RuntimeError("source maintenance vhost must not contain an application upstream")
    return rendered


def _atomic_symlink_replace(link: Path, target: str) -> None:
    if link.parent.resolve(strict=False) != NGINX_SITES_ENABLED.resolve(strict=False):
        raise RuntimeError("refusing to replace a symlink outside nginx sites-enabled")
    tmp = link.with_name(f".{link.name}.{secrets.token_hex(6)}.tmp")
    try:
        os.symlink(target, tmp)
        os.replace(tmp, link)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _nginx_reload_checked() -> None:
    _run_checked(["nginx", "-t"], timeout_sec=30)
    _run_checked(["systemctl", "reload", "nginx"], timeout_sec=30)


def _drain_reloaded_nginx_workers(*, timeout_sec: int = 120, stable_snapshots: int = 3) -> dict[str, Any]:
    deadline = time.monotonic() + max(1, int(timeout_sec))
    stable = 0
    attempts = 0
    remaining: list[int] = []
    while True:
        attempts += 1
        rc, output = _run(["ps", "-eo", "pid=,args="], timeout_sec=10)
        if rc != 0:
            raise RuntimeError("cannot observe draining nginx workers")
        remaining = []
        for raw in output.splitlines():
            if "nginx: worker process is shutting down" not in raw:
                continue
            try:
                remaining.append(int(raw.strip().split(None, 1)[0]))
            except (ValueError, IndexError):
                continue
        if remaining:
            stable = 0
        else:
            stable += 1
        if stable >= max(2, int(stable_snapshots)):
            return {
                "ok": True,
                "attempts": attempts,
                "stable_snapshots": stable,
                "stable_snapshots_required": max(2, int(stable_snapshots)),
                "remaining_worker_pids": [],
            }
        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "attempts": attempts,
                "stable_snapshots": stable,
                "stable_snapshots_required": max(2, int(stable_snapshots)),
                "remaining_worker_pids": remaining,
            }
        time.sleep(0.2)


def _source_original_symlink_target(state: dict[str, Any]) -> str:
    canonical = str(state.get("original_symlink_target") or "")
    if not canonical:
        raise RuntimeError("source migration original symlink target evidence is missing")
    link_raw = str(state.get("active_link") or "")
    config_raw = str(state.get("original_config") or "")
    if not link_raw or not config_raw:
        raise RuntimeError("source migration original symlink snapshot path evidence is missing")
    link = Path(link_raw)
    resolved_target = (link.parent / canonical).resolve(strict=False)
    if resolved_target != Path(config_raw).resolve(strict=False):
        raise RuntimeError("source migration original symlink target evidence conflicts with original config")
    return canonical


def _source_original_preserved(state: dict[str, Any]) -> dict[str, Any]:
    config_path = Path(str(state.get("original_config") or ""))
    link = Path(str(state.get("active_link") or ""))
    expected_hash = str(state.get("original_config_sha256") or "")
    actual_hash = _sha256_file(config_path) if config_path.is_file() else ""
    original_symlink_target = _source_original_symlink_target(state)
    return {
        "original_config": str(config_path),
        "original_config_exists": config_path.is_file(),
        "original_config_sha256": actual_hash,
        "original_config_unchanged": bool(expected_hash and actual_hash == expected_hash),
        "original_link": str(link),
        "original_symlink_target": original_symlink_target,
        "source_root_exists": Path(str(state.get("source_root") or "")).is_dir(),
    }


def _validate_source_migration_state(
    state: dict[str, Any],
    *,
    selector: str | None,
    domains: list[str],
    instance_uid: str,
    instance_code: str,
) -> None:
    uid = validate_mcc_instance_uid(instance_uid)
    code = validate_instance_code(instance_code)
    if str(state.get("instance_uid") or "") != uid or str(state.get("instance_code") or "") != code:
        raise RuntimeError("source migration maintenance identity does not match request")
    if list(state.get("domains") or []) != domains:
        raise RuntimeError("source migration maintenance domains do not match request")
    token = str(selector or "").strip()
    if token and Path(token).is_absolute() and Path(token).resolve() != Path(str(state.get("source_root") or "")).resolve():
        raise RuntimeError("source migration maintenance root does not match request")
    _source_original_symlink_target(state)


def _source_maintenance_link_active(state: dict[str, Any]) -> bool:
    link = Path(str(state.get("active_link") or ""))
    if not link.is_symlink():
        return False
    return (link.parent / os.readlink(link)).resolve(strict=False) == Path(
        str(state.get("maintenance_config") or "")
    ).resolve(strict=False)


def _source_original_link_active(state: dict[str, Any]) -> bool:
    link = Path(str(state.get("active_link") or ""))
    try:
        expected = _source_original_symlink_target(state)
    except RuntimeError:
        return False
    return bool(expected and link.is_symlink() and os.readlink(link) == expected)


def _source_maintenance_vhost_is_static(state: dict[str, Any]) -> bool:
    path = Path(str(state.get("maintenance_config") or ""))
    try:
        content = path.read_text(encoding="utf-8", errors="strict").lower()
    except (OSError, UnicodeError):
        return False
    prohibited = ("fastcgi", "proxy_pass", "uwsgi", "scgi_pass", "php")
    return "return 503" in content and not any(token in content for token in prohibited)


def _restore_source_runtime(config: AgentConfig, state: dict[str, Any]) -> dict[str, Any]:
    pause_path = Path(str(getattr(config, "scheduler_pause_flag_path", "/opt/mcd/var/scheduler.pause") or "/opt/mcd/var/scheduler.pause"))
    pause_restored = True
    pause_error = ""
    if not bool(state.get("pause_preexisting", False)) and pause_path.exists():
        try:
            pause_path.unlink()
        except OSError as exc:
            pause_restored = False
            pause_error = str(exc)
    if bool(state.get("cron_marker_preexisting", False)):
        cron_restore: dict[str, Any] = {
            "ok": True,
            "requested": False,
            "message": "pre-existing cron maintenance state preserved",
        }
    else:
        cron_restore = restore_cron_service_if_needed(config)
    return {
        "ok": bool(pause_restored and cron_restore.get("ok", True)),
        "pause_restored": pause_restored,
        "pause_error": pause_error,
        "cron_restore": cron_restore,
    }


def _restore_source_vhost(state: dict[str, Any]) -> dict[str, Any]:
    evidence = _source_original_preserved(state)
    if not evidence["original_config_unchanged"]:
        raise RuntimeError("original source nginx config is missing or changed; automatic restore refused")
    link = Path(str(state.get("active_link") or ""))
    original_target = _source_original_symlink_target(state)
    _atomic_symlink_replace(link, original_target)
    try:
        _nginx_reload_checked()
    except Exception:
        maintenance_target = str(state.get("maintenance_config") or "")
        if maintenance_target:
            _atomic_symlink_replace(link, maintenance_target)
            try:
                _nginx_reload_checked()
            except Exception:
                pass
        raise
    return {
        **evidence,
        "original_link_restored": link.is_symlink() and os.readlink(link) == original_target,
    }


def _source_quiescent_receipt(state: dict[str, Any]) -> dict[str, Any]:
    preservation = _source_original_preserved(state)
    static_503 = _source_maintenance_vhost_is_static(state)
    return {
        "schema": "mcd-instance-migration-source-quiescence-receipt-v1",
        "ok": bool(
            state.get("state") == "quiescent"
            and _source_maintenance_link_active(state)
            and preservation.get("original_config_unchanged")
            and preservation.get("original_symlink_target")
            and state.get("source_quiescent")
            and static_503
        ),
        "phase": str(state.get("state") or "unknown"),
        "source_quiescent": bool(state.get("source_quiescent", False)),
        "source_authoritative": True,
        "source_root": str(state.get("source_root") or ""),
        "domains": list(state.get("domains") or []),
        "instance_uid": str(state.get("instance_uid") or ""),
        "instance_code": str(state.get("instance_code") or ""),
        "identity_adoption": dict(state.get("identity_adoption") or {}),
        "maintenance_vhost_active": _source_maintenance_link_active(state),
        "maintenance_vhost_static_503": static_503,
        "task_drain": dict(state.get("task_drain") or {}),
        "web_request_drain": dict(state.get("web_request_drain") or {}),
        "preservation": preservation,
    }


def _quiesce_migration_source_impl(
    config: AgentConfig,
    *,
    selector: str | None,
    domains_json: str,
    instance_uid: str,
    instance_code: str,
) -> dict[str, Any]:
    domains = _json_domains(domains_json)
    _inst, root, identity, identity_adoption = _migration_source_identity(
        config,
        selector=selector,
        domains=domains,
        instance_uid=instance_uid,
        instance_code=instance_code,
    )
    state_path = _source_migration_state_path(instance_uid)
    if state_path.is_file():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        if isinstance(existing, dict) and existing.get("schema") == MIGRATION_SOURCE_STATE_SCHEMA:
            _validate_source_migration_state(
                existing,
                selector=selector,
                domains=domains,
                instance_uid=instance_uid,
                instance_code=instance_code,
            )
            if existing.get("state") == "quiescent":
                receipt = _source_quiescent_receipt(existing)
                if receipt["ok"]:
                    return receipt
            if existing.get("state") not in {"restored", "finalized"}:
                raise RuntimeError("source migration maintenance already has an unfinished state")

    link, original_symlink_target, original_config, original_content = _find_source_vhost(root, domains)
    uid = validate_mcc_instance_uid(instance_uid)
    code = validate_instance_code(instance_code)
    maintenance_dir = MIGRATION_SOURCE_MAINTENANCE_ROOT / uid
    page_path = maintenance_dir / "mcd-migration-maintenance.html"
    maintenance_config = NGINX_SITES_AVAILABLE / f"zz-mcd-migration-maintenance-{code}.conf"
    maintenance_content = _render_source_maintenance_vhost(
        original=original_content,
        domains=domains,
        page_root=maintenance_dir,
    )
    maintenance_state = collect_maintenance_state(config)
    pause_path = Path(str(getattr(config, "scheduler_pause_flag_path", "/opt/mcd/var/scheduler.pause") or "/opt/mcd/var/scheduler.pause"))
    state: dict[str, Any] = {
        "schema": MIGRATION_SOURCE_STATE_SCHEMA,
        "state": "preparing",
        "created_at_utc": _utc_now(),
        "source_root": str(root),
        "domains": domains,
        "instance_uid": uid,
        "instance_code": code,
        "identity": identity,
        "identity_adoption": identity_adoption,
        "active_link": str(link),
        "original_symlink_target": original_symlink_target,
        "original_config": str(original_config),
        "original_config_sha256": _sha256_file(original_config),
        "maintenance_config": str(maintenance_config),
        "maintenance_page": str(page_path),
        "pause_preexisting": bool(pause_path.exists()),
        "cron_marker_preexisting": bool(maintenance_state.get("cron_marker_present", False)),
        "source_quiescent": False,
    }
    _write_source_migration_state(state_path, state)
    switched = False
    runtime_changed = False
    try:
        _atomic_write_text(page_path, _maintenance_page(), mode=0o644)
        _atomic_write_text(maintenance_config, maintenance_content, mode=0o644)
        _run_checked(["nginx", "-t"], timeout_sec=30)
        _atomic_symlink_replace(link, str(maintenance_config))
        switched = True
        _nginx_reload_checked()

        pause_path.parent.mkdir(parents=True, exist_ok=True)
        if not pause_path.exists():
            _atomic_write_text(pause_path, "paused\n", mode=0o600)
        runtime_changed = True
        if bool(state["cron_marker_preexisting"]):
            cron_stop: dict[str, Any] = {
                "ok": True,
                "requested": False,
                "message": "pre-existing cron maintenance state preserved",
            }
        else:
            cron_stop = stop_cron_service(config)
        if not bool(cron_stop.get("ok", False)):
            raise RuntimeError("source cron stop failed: " + str(cron_stop.get("message") or "unknown error"))
        task_drain = stop_running_tasks_for_maintenance(
            config,
            grace_sec=30,
            kill_orphans=True,
            stable_snapshots=3,
            drain_timeout_sec=120,
        )
        if not bool(task_drain.get("ok", False)):
            raise RuntimeError("source Mautic background tasks did not become quiescent")
        web_drain = _drain_reloaded_nginx_workers(timeout_sec=120, stable_snapshots=3)
        if not bool(web_drain.get("ok", False)):
            raise RuntimeError("source PHP/web requests did not drain before timeout")

        state.update(
            {
                "state": "quiescent",
                "source_quiescent": True,
                "quiescent_at_utc": _utc_now(),
                "cron_stop": cron_stop,
                "task_drain": task_drain,
                "web_request_drain": web_drain,
            }
        )
        _write_source_migration_state(state_path, state)
        return _source_quiescent_receipt(state)
    except Exception as exc:
        restore_errors: list[str] = []
        if switched:
            try:
                _restore_source_vhost(state)
            except Exception as restore_exc:
                restore_errors.append(f"vhost: {restore_exc}")
        if runtime_changed:
            runtime_restore = _restore_source_runtime(config, state)
            if not bool(runtime_restore.get("ok", False)):
                restore_errors.append("runtime: " + json.dumps(runtime_restore, ensure_ascii=True))
        state.update(
            {
                "state": "failed",
                "source_quiescent": False,
                "failed_at_utc": _utc_now(),
                "error": _safe_runtime_detail(exc),
                "restore_errors": restore_errors,
            }
        )
        _write_source_migration_state(state_path, state)
        return {
            "schema": "mcd-instance-migration-source-quiescence-receipt-v1",
            "ok": False,
            "phase": "source_quiesce_failed",
            "source_quiescent": False,
            "source_authoritative": _source_original_link_active(state),
            "source_root": str(state.get("source_root") or ""),
            "domains": list(state.get("domains") or []),
            "instance_uid": str(state.get("instance_uid") or ""),
            "instance_code": str(state.get("instance_code") or ""),
            "identity_adoption": dict(state.get("identity_adoption") or {}),
            "maintenance_vhost_active": _source_maintenance_link_active(state),
            "maintenance_vhost_static_503": _source_maintenance_vhost_is_static(state),
            "task_drain": dict(state.get("task_drain") or {}),
            "web_request_drain": dict(state.get("web_request_drain") or {}),
            "error": _safe_runtime_detail(exc),
            "restore_errors": restore_errors,
            "rollback_required": bool(restore_errors),
            "preservation": _source_original_preserved(state),
        }


def _restore_migration_source_impl(
    config: AgentConfig,
    *,
    selector: str | None,
    domains_json: str,
    instance_uid: str,
    instance_code: str,
) -> dict[str, Any]:
    domains = _json_domains(domains_json)
    state_path, state = _read_source_migration_state(instance_uid)
    _validate_source_migration_state(
        state,
        selector=selector,
        domains=domains,
        instance_uid=instance_uid,
        instance_code=instance_code,
    )
    if state.get("state") == "finalized":
        raise RuntimeError("source is already finalized as a recoverable artifact")
    vhost = _restore_source_vhost(state)
    runtime_restore = _restore_source_runtime(config, state)
    if not bool(runtime_restore.get("ok", False)):
        raise RuntimeError("source runtime state restore failed: " + json.dumps(runtime_restore, ensure_ascii=True))
    state.update(
        {
            "state": "restored",
            "source_quiescent": False,
            "restored_at_utc": _utc_now(),
            "vhost_restore": vhost,
            "runtime_restore": runtime_restore,
        }
    )
    _write_source_migration_state(state_path, state)
    return {
        "schema": "mcd-instance-migration-source-restore-receipt-v1",
        "ok": True,
        "phase": "source_restored",
        "source_authoritative": True,
        "source_quiescent": False,
        "original_vhost_active": True,
        "source_root": str(state.get("source_root") or ""),
        "domains": domains,
        "instance_uid": str(state.get("instance_uid") or ""),
        "instance_code": str(state.get("instance_code") or ""),
        "identity_adoption": dict(state.get("identity_adoption") or {}),
        "preservation": _source_original_preserved(state),
        "runtime_restore": runtime_restore,
    }


def _finalize_migration_source_impl(
    config: AgentConfig,
    *,
    selector: str | None,
    domains_json: str,
    instance_uid: str,
    instance_code: str,
) -> dict[str, Any]:
    domains = _json_domains(domains_json)
    state_path, state = _read_source_migration_state(instance_uid)
    _validate_source_migration_state(
        state,
        selector=selector,
        domains=domains,
        instance_uid=instance_uid,
        instance_code=instance_code,
    )
    if state.get("state") not in {"quiescent", "artifact_pending_runtime_restore", "finalized"}:
        raise RuntimeError("source is not in a finalizable quiescent state")
    preservation = _source_original_preserved(state)
    if not bool(preservation.get("original_config_unchanged")) or not _source_maintenance_link_active(state):
        raise RuntimeError("source preservation or maintenance-vhost evidence is invalid")
    root = Path(str(state.get("source_root") or "")).resolve()
    artifact_state = write_instance_state(
        root,
        profile="artifact",
        instance_uid=instance_uid,
        instance_code=instance_code,
        primary_domain=domains[0],
        ownership_confirmed=True,
    )
    state.update(
        {
            "state": "artifact_pending_runtime_restore",
            "source_quiescent": True,
            "artifact_state": artifact_state,
            "artifact_at_utc": str(state.get("artifact_at_utc") or _utc_now()),
        }
    )
    _write_source_migration_state(state_path, state)
    runtime_restore = _restore_source_runtime(config, state)
    if not bool(runtime_restore.get("ok", False)):
        state["runtime_restore"] = runtime_restore
        _write_source_migration_state(state_path, state)
        return {
            "schema": "mcd-instance-migration-source-finalize-receipt-v1",
            "ok": False,
            "phase": "artifact_pending_runtime_restore",
            "migration_complete": False,
            "source_authoritative": False,
            "source_quiescent": True,
            "recoverable_artifact": True,
            "maintenance_vhost_active": _source_maintenance_link_active(state),
            "source_root": str(root),
            "domains": domains,
            "instance_uid": validate_mcc_instance_uid(instance_uid),
            "instance_code": validate_instance_code(instance_code),
            "identity_adoption": dict(state.get("identity_adoption") or {}),
            "artifact_state": artifact_state,
            "runtime_restore": runtime_restore,
            "preservation": preservation,
            "error": "source runtime state restore is pending",
            "retryable": True,
        }
    state.update(
        {
            "state": "finalized",
            "finalized_at_utc": _utc_now(),
            "runtime_restore": runtime_restore,
        }
    )
    _write_source_migration_state(state_path, state)
    inventory_warning = ""
    try:
        InstanceInventory(config.state_db_path).rescan(config)
    except Exception as exc:
        inventory_warning = _safe_runtime_detail(exc)
    receipt = {
        "schema": "mcd-instance-migration-source-finalize-receipt-v1",
        "ok": True,
        "phase": "complete",
        "migration_complete": True,
        "source_authoritative": False,
        "source_quiescent": True,
        "recoverable_artifact": True,
        "maintenance_vhost_active": True,
        "source_root": str(root),
        "domains": domains,
        "instance_uid": validate_mcc_instance_uid(instance_uid),
        "instance_code": validate_instance_code(instance_code),
        "identity_adoption": dict(state.get("identity_adoption") or {}),
        "artifact_state": artifact_state,
        "preservation": _source_original_preserved(state),
        "runtime_restore": runtime_restore,
    }
    if inventory_warning:
        receipt["inventory_warning"] = inventory_warning
    return receipt


def _source_operation_failure_receipt(
    operation: str,
    exc: Exception,
    *,
    selector: str | None,
    domains_json: str,
    instance_uid: str,
    instance_code: str,
) -> dict[str, Any]:
    try:
        domains = _json_domains(domains_json)
    except Exception:
        domains = []
    state: dict[str, Any] = {}
    try:
        _path, state = _read_source_migration_state(instance_uid)
    except Exception:
        state = {}
    source_root = str(state.get("source_root") or selector or "")
    preservation: dict[str, Any] = {}
    if state:
        try:
            preservation = _source_original_preserved(state)
        except Exception:
            preservation = {}
    error = _safe_runtime_detail(exc)
    if operation == "source-quiesce":
        restore_errors = list(state.get("restore_errors") or [])
        maintenance_active = bool(state and _source_maintenance_link_active(state))
        original_active = not state or _source_original_link_active(state)
        return {
            "schema": "mcd-instance-migration-source-quiescence-receipt-v1",
            "ok": False,
            "phase": "source_quiesce_failed",
            "source_quiescent": False,
            "source_authoritative": original_active,
            "source_root": source_root,
            "domains": domains,
            "instance_uid": str(instance_uid or ""),
            "instance_code": str(instance_code or ""),
            "identity_adoption": dict(state.get("identity_adoption") or {}),
            "maintenance_vhost_active": maintenance_active,
            "maintenance_vhost_static_503": bool(state and _source_maintenance_vhost_is_static(state)),
            "task_drain": dict(state.get("task_drain") or {}),
            "web_request_drain": dict(state.get("web_request_drain") or {}),
            "preservation": preservation,
            "error": error,
            "restore_errors": restore_errors,
            "rollback_required": bool(restore_errors or maintenance_active),
        }
    if operation == "source-restore":
        original_active = bool(state and _source_original_link_active(state))
        return {
            "schema": "mcd-instance-migration-source-restore-receipt-v1",
            "ok": False,
            "phase": "source_restore_failed",
            "source_authoritative": original_active,
            "source_quiescent": bool(state.get("source_quiescent", False)),
            "original_vhost_active": original_active,
            "source_root": source_root,
            "domains": domains,
            "instance_uid": str(instance_uid or ""),
            "instance_code": str(instance_code or ""),
            "identity_adoption": dict(state.get("identity_adoption") or {}),
            "preservation": preservation,
            "runtime_restore": dict(state.get("runtime_restore") or {}),
            "error": error,
            "rollback_required": True,
            "retryable": True,
        }
    return {
        "schema": "mcd-instance-migration-source-finalize-receipt-v1",
        "ok": False,
        "phase": "source_finalize_failed",
        "migration_complete": False,
        "source_authoritative": False,
        "source_quiescent": bool(state.get("source_quiescent", False)),
        "recoverable_artifact": bool(state.get("artifact_state")),
        "maintenance_vhost_active": bool(state and _source_maintenance_link_active(state)),
        "source_root": source_root,
        "domains": domains,
        "instance_uid": str(instance_uid or ""),
        "instance_code": str(instance_code or ""),
        "identity_adoption": dict(state.get("identity_adoption") or {}),
        "artifact_state": dict(state.get("artifact_state") or {}),
        "preservation": preservation,
        "runtime_restore": dict(state.get("runtime_restore") or {}),
        "error": error,
        "rollback_required": False,
        "retryable": True,
    }


@stable_source_operation
def quiesce_migration_source(
    config: AgentConfig,
    *,
    selector: str | None,
    domains_json: str,
    instance_uid: str,
    instance_code: str,
) -> dict[str, Any]:
    try:
        return _quiesce_migration_source_impl(
            config,
            selector=selector,
            domains_json=domains_json,
            instance_uid=instance_uid,
            instance_code=instance_code,
        )
    except Exception as exc:
        return _source_operation_failure_receipt(
            "source-quiesce",
            exc,
            selector=selector,
            domains_json=domains_json,
            instance_uid=instance_uid,
            instance_code=instance_code,
        )


@stable_source_operation
def restore_migration_source(
    config: AgentConfig,
    *,
    selector: str | None,
    domains_json: str,
    instance_uid: str,
    instance_code: str,
) -> dict[str, Any]:
    try:
        return _restore_migration_source_impl(
            config,
            selector=selector,
            domains_json=domains_json,
            instance_uid=instance_uid,
            instance_code=instance_code,
        )
    except Exception as exc:
        return _source_operation_failure_receipt(
            "source-restore",
            exc,
            selector=selector,
            domains_json=domains_json,
            instance_uid=instance_uid,
            instance_code=instance_code,
        )


@stable_source_operation
def finalize_migration_source(
    config: AgentConfig,
    *,
    selector: str | None,
    domains_json: str,
    instance_uid: str,
    instance_code: str,
) -> dict[str, Any]:
    try:
        return _finalize_migration_source_impl(
            config,
            selector=selector,
            domains_json=domains_json,
            instance_uid=instance_uid,
            instance_code=instance_code,
        )
    except Exception as exc:
        return _source_operation_failure_receipt(
            "source-finalize",
            exc,
            selector=selector,
            domains_json=domains_json,
            instance_uid=instance_uid,
            instance_code=instance_code,
        )


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


def _patch_local_php_instance_paths(root: Path) -> list[str]:
    path = _local_php_path(root)
    root = root.resolve()
    media_root = _nginx_web_root(root) / "media" / "files"
    replacements = {
        "cache_path": root / "var" / "cache",
        "log_path": root / "var" / "logs",
        "tmp_path": root / "var" / "tmp",
        "import_campaigns_dir": root / "var" / "import",
        "import_leads_dir": root / "var" / "import",
        "upload_dir": media_root,
        "contact_export_dir": media_root / "temp",
        "report_temp_dir": media_root / "temp",
        "form_upload_dir": media_root / "form",
    }
    text = path.read_text(encoding="utf-8", errors="ignore")
    changed: list[str] = []
    for key, value_path in replacements.items():
        value = str(value_path)
        pattern = rf"(['\"]{re.escape(key)}['\"]\s*=>\s*)['\"][^'\"]*['\"]"
        new_text, count = re.subn(pattern, lambda m, v=value: m.group(1) + _quote_sql(v), text, count=1)
        if count > 0 and new_text != text:
            changed.append(key)
            text = new_text
    if changed:
        path.write_text(text, encoding="utf-8")
        for value_path in replacements.values():
            value_path.mkdir(parents=True, exist_ok=True)
    return changed


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


def _assert_safe_target_wipe_path(target: Path) -> None:
    resolved = target.resolve()
    protected = {
        Path("/"),
        Path("/var"),
        Path("/var/www"),
        Path("/etc"),
        Path("/usr"),
        Path("/opt"),
        Path("/home"),
        Path("/root"),
        Path("/tmp"),
    }
    if resolved in protected:
        raise RuntimeError(f"refusing to wipe protected target path: {resolved}")
    if len(resolved.parts) < 3:
        raise RuntimeError(f"refusing to wipe shallow target path: {resolved}")


def preflight_target_relay(
    *,
    target_root: str,
    target_db_name: str,
    wipe_target_root: bool = False,
    wipe_target_db: bool = False,
    runtime_adapter: str | None = None,
    runtime: str | None = None,
    install_type: str | None = None,
    image_ref: str | None = None,
    domains_json: str = "[]",
    instance_uid: str | None = None,
    instance_code: str | None = None,
) -> dict[str, Any]:
    target = Path(target_root).resolve()
    if not str(target).startswith("/"):
        raise RuntimeError("target root must be absolute")
    domains = _json_domains(domains_json)
    if bool(instance_uid) != bool(instance_code):
        raise RuntimeError("instance_uid and instance_code must be supplied together by MCC")
    if instance_uid:
        validate_mcc_instance_uid(instance_uid)
    if instance_code:
        validate_instance_code(instance_code)
        identity_names = instance_path_names(domains[0], instance_code) if domains else {}
        target_db_name = str(target_db_name or identity_names.get("db_name") or "")
    problems: list[str] = []
    cleanup: list[str] = []
    adapter_result: dict[str, Any] = {}
    required_commands = ("tar", "gzip") if runtime_adapter else ("tar", "gzip", "nginx", "php")
    for cmd in required_commands:
        if not shutil.which(cmd):
            problems.append(f"missing command: {cmd}")
    if not _DB_IDENT_RE.match(str(target_db_name or "")):
        problems.append(f"invalid target database name: {target_db_name}")
    if runtime_adapter:
        try:
            runtime_adapter_path(runtime_adapter)
            adapter_result = run_runtime_adapter(
                runtime_adapter,
                operation="target-preflight",
                payload={
                    "target_root": str(target),
                    "target_db_name": str(target_db_name or ""),
                    "runtime": str(runtime or "host").strip().lower() or "host",
                    "install_type": str(install_type or "unknown").strip().lower() or "unknown",
                    "image_ref": str(image_ref or ""),
                    "domains": _json_domains(domains_json),
                    "instance_uid": str(instance_uid or ""),
                    "instance_code": str(instance_code or ""),
                },
                timeout_sec=120,
            )
        except Exception as exc:
            problems.append(f"runtime adapter preflight failed: {exc}")

    # A missing/untrusted adapter or image must never trigger destructive
    # cleanup.  Validate the complete runtime route first, then process the
    # operator's explicit wipe flags.
    if problems:
        return {
            "schema": "mcd-instance-migration-target-preflight-v1",
            "ok": False,
            "target_root": str(target),
            "target_db_name": str(target_db_name or ""),
            "domains": domains,
            "instance_uid": str(instance_uid or ""),
            "instance_code": str(instance_code or ""),
            "wipe_target_root": bool(wipe_target_root),
            "wipe_target_db": bool(wipe_target_db),
            "cleanup": cleanup,
            "runtime_adapter": str(runtime_adapter or ""),
            "adapter": adapter_result,
            "problems": problems,
        }
    try:
        if _path_is_below(target, MIGRATION_ROOT):
            if wipe_target_root:
                if not instance_uid or not instance_code:
                    raise RuntimeError("exact managed target cleanup requires instance_uid and instance_code")
                removed = _remove_exact_migration_target(
                    target,
                    domains=domains,
                    instance_uid=instance_uid,
                    instance_code=instance_code,
                )
                if removed:
                    cleanup.append(f"target root removed: {target}")
            else:
                archived = _prepare_migration_target(
                    target,
                    domains=domains,
                    claim=False,
                    instance_uid=instance_uid,
                    instance_code=instance_code,
                )
                if archived is not None:
                    cleanup.append(f"target root archived: {target} -> {archived}")
        elif target.exists() and any(target.iterdir()):
            if wipe_target_root:
                _assert_safe_target_wipe_path(target)
                shutil.rmtree(target)
                cleanup.append(f"target root removed: {target}")
            else:
                problems.append(f"target root already exists and is not empty: {target}")
    except Exception as exc:
        problems.append(f"target root cleanup failed: {exc}")
    if _DB_IDENT_RE.match(str(target_db_name or "")):
        try:
            if _target_db_exists(str(target_db_name)):
                if wipe_target_db:
                    _mysql_exec(f"DROP DATABASE IF EXISTS {_quote_ident(str(target_db_name))}")
                    cleanup.append(f"target database dropped: {target_db_name}")
                else:
                    problems.append(f"target database already exists: {target_db_name}")
        except Exception as exc:
            problems.append(f"target database preflight failed: {exc}")
    return {
        "schema": "mcd-instance-migration-target-preflight-v1",
        "ok": not problems,
        "target_root": str(target),
        "target_db_name": str(target_db_name or ""),
        "domains": domains,
        "instance_uid": str(instance_uid or ""),
        "instance_code": str(instance_code or ""),
        "identity": identity_readback(
            target,
            instance_uid=instance_uid,
            instance_code=instance_code,
            primary_domain=domains[0] if domains else None,
        ),
        "wipe_target_root": bool(wipe_target_root),
        "wipe_target_db": bool(wipe_target_db),
        "cleanup": cleanup,
        "runtime_adapter": str(runtime_adapter or ""),
        "adapter": adapter_result,
        "problems": problems,
    }


def _target_db_exists(db_name: str) -> bool:
    safe = str(db_name or "").replace("'", "''")
    out = _mysql_exec(f"SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME='{safe}'")
    return bool(str(out or "").strip())


@stable_source_operation
def stream_source_files(config: AgentConfig, *, selector: str | None, output: Any) -> int:
    inv = InstanceInventory(config.state_db_path)
    ensure_seeded(inv, config)
    inst = _select_instance(inv.list_instances(), selector, host_name=_mcc_wire_host_name(config))
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
        "--exclude=./.mcd-runtime.json",
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


def receive_target_files(
    *,
    target_root: str,
    input_stream: Any,
    wipe_target: bool = False,
    domains_json: str = "[]",
    instance_uid: str | None = None,
    instance_code: str | None = None,
) -> dict[str, Any]:
    target = Path(target_root).resolve()
    if not str(target).startswith("/"):
        raise RuntimeError("target root must be absolute")
    domains = _json_domains(domains_json)
    if bool(instance_uid) != bool(instance_code):
        raise RuntimeError("instance_uid and instance_code must be supplied together by MCC")
    if instance_uid:
        validate_mcc_instance_uid(instance_uid)
    if instance_code:
        validate_instance_code(instance_code)
    archived: Path | None = None
    if _path_is_below(target, MIGRATION_ROOT):
        archived = _prepare_migration_target(
            target,
            domains=domains,
            claim=True,
            instance_uid=instance_uid,
            instance_code=instance_code,
        )
    else:
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
    if instance_uid and instance_code:
        write_instance_identity(
            target,
            instance_uid=instance_uid,
            instance_code=instance_code,
            primary_domain=domains[0] if domains else None,
        )
    return {
        "schema": "mcd-instance-migration-target-files-v1",
        "ok": True,
        "target_root": str(target),
        "wipe_target": bool(wipe_target),
        "target_archive_root": str(archived) if archived is not None else "",
        "instance_uid": str(instance_uid or ""),
        "instance_code": str(instance_code or ""),
        "identity": identity_readback(
            target,
            instance_uid=instance_uid,
            instance_code=instance_code,
            primary_domain=domains[0] if domains else None,
        ),
    }


def _dump_supports_network_timeout(dump_bin: str) -> bool:
    """Return whether this dump client exposes MySQL's large-table timeout switch."""
    return _dump_supports_option(dump_bin, "--network-timeout")


def _dump_supports_option(dump_bin: str, option: str) -> bool:
    """Return whether a dump client exposes a named client option."""
    try:
        proc = subprocess.run(
            [dump_bin, "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=10,
        )
    except Exception:
        return False
    help_text = (proc.stdout or b"").decode("utf-8", errors="replace")
    return str(option) in help_text


def _db_stream_marker(phase: str, stream_id: str) -> bytes:
    return f"-- MCD-DB-STREAM-V1 {phase} {stream_id}\n".encode("ascii")


def _verify_db_stream_file(path: Path) -> str:
    """Verify gzip integrity and the source completion fence before DB mutation."""
    first_line = b""
    last_nonempty = b""
    try:
        with gzip.open(path, mode="rb") as gz:
            first_line = gz.readline()
            last_nonempty = first_line if first_line.strip() else b""
            while True:
                line = gz.readline()
                if not line:
                    break
                if line.strip():
                    last_nonempty = line
    except (EOFError, OSError) as exc:
        raise RuntimeError(f"database stream gzip is incomplete or corrupt: {exc}") from exc

    begin = _DB_STREAM_MARKER_RE.fullmatch(first_line)
    complete = _DB_STREAM_MARKER_RE.fullmatch(last_nonempty)
    if begin is None or begin.group("phase") != b"BEGIN":
        raise RuntimeError("database stream begin marker is missing")
    if complete is None or complete.group("phase") != b"COMPLETE":
        raise RuntimeError("database stream completion marker is missing")
    stream_id = begin.group("stream_id")
    if complete.group("stream_id") != stream_id:
        raise RuntimeError("database stream completion marker does not match begin marker")
    return stream_id.decode("ascii")


def _wait_stream_process(proc: subprocess.Popen[Any], *, terminate: bool = False) -> int:
    """Stop a relay child cleanly, with a bounded shutdown wait."""
    if terminate and proc.poll() is None:
        proc.terminate()
    try:
        return int(proc.wait(timeout=30))
    except TypeError:
        # Small test doubles and older wrappers may not accept timeout=.
        return int(proc.wait())
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            return int(proc.wait(timeout=30))
        except TypeError:
            return int(proc.wait())


def _stream_begin_id(line: bytes) -> str:
    match = _DB_STREAM_MARKER_RE.fullmatch(line)
    if match is None or match.group("phase") != b"BEGIN":
        raise RuntimeError("database stream begin marker is missing")
    return match.group("stream_id").decode("ascii")


def _iter_verified_db_stream_payload(lines: Any, *, stream_id: str) -> Any:
    """Yield SQL while withholding the final completion fence from mysql."""
    # Keep the last non-empty line plus following whitespace in a small tail.
    # This lets the target validate the completion fence without buffering the
    # database dump and without sending the fence to mysql.
    tail: list[bytes] = []
    for raw in lines:
        line = raw if isinstance(raw, bytes) else str(raw).encode("utf-8", errors="surrogateescape")
        if line.strip():
            if tail:
                yield from tail
            tail = [line]
        elif tail:
            tail.append(line)
        else:
            yield line

    if not tail:
        raise RuntimeError("database stream completion marker is missing")
    complete = _DB_STREAM_MARKER_RE.fullmatch(tail[0])
    if complete is None or complete.group("phase") != b"COMPLETE":
        raise RuntimeError("database stream completion marker is missing")
    if complete.group("stream_id").decode("ascii") != stream_id:
        raise RuntimeError("database stream completion marker does not match begin marker")
    if any(line.strip() for line in tail[1:]):
        raise RuntimeError("database stream has data after completion marker")


@stable_source_operation
def stream_source_db(config: AgentConfig, *, selector: str | None, output: Any) -> int:
    inv = InstanceInventory(config.state_db_path)
    ensure_seeded(inv, config)
    inst = _select_instance(inv.list_instances(), selector, host_name=_mcc_wire_host_name(config))
    # Runtime descriptors translate container-only DB endpoints to the
    # host-side address MCD can actually reach.  Re-reading local.php here
    # would discard that translation (for example 172.30.0.1 -> 127.0.0.1)
    # and makes Docker migrations fail even though inventory and probes work.
    source_db = inst.db or _db_from_local_php(Path(inst.root).resolve())
    if not _is_local_db_host(source_db.host):
        raise RuntimeError(
            f"only a host-local source DB is supported for migration, got db_host={source_db.host}"
        )
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
        dump_bin = shutil.which("mariadb-dump") or shutil.which("mysqldump")
        if not dump_bin:
            raise RuntimeError("mariadb-dump or mysqldump is required for source database stream")
        stream_id = secrets.token_hex(16)
        stderr_path = Path(td) / "mariadb-dump.stderr"
        dump_argv = [
            dump_bin,
            f"--defaults-extra-file={defaults}",
            "--single-transaction",
            "--quick",
            "--max-allowed-packet=1073741824",
            "--routines",
            "--triggers",
        ]
        if _dump_supports_network_timeout(dump_bin):
            dump_argv.append("--network-timeout")
        if _dump_supports_option(dump_bin, "--init-command"):
            # A direct relay is intentionally backpressured by the target.
            # Keep the source MariaDB session alive while a slow target
            # consumer catches up; this is session-scoped and does not alter
            # the source server or other clients.
            dump_argv.append(
                "--init-command=SET SESSION net_write_timeout=31536000, net_read_timeout=31536000"
            )
        dump_argv.append(source_db.name)

        with stderr_path.open("wb") as stderr_file:
            proc = subprocess.Popen(
                dump_argv,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
            )
            assert proc.stdout is not None
            relay_error: BaseException | None = None
            rc = 0
            try:
                with gzip.GzipFile(fileobj=output, mode="wb", compresslevel=1) as gz:
                    try:
                        gz.write(_db_stream_marker("BEGIN", stream_id))
                        shutil.copyfileobj(proc.stdout, gz, length=_DB_STREAM_COPY_BUFFER_BYTES)
                    except BaseException as exc:
                        relay_error = exc
                    if relay_error is not None:
                        rc = _wait_stream_process(proc, terminate=True)
                    else:
                        rc = _wait_stream_process(proc)
                        if rc == 0:
                            gz.write(b"\n")
                            gz.write(_db_stream_marker("COMPLETE", stream_id))
            except BaseException as exc:
                if relay_error is None:
                    relay_error = exc
                if proc.poll() is None:
                    rc = _wait_stream_process(proc, terminate=True)
            finally:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

        err = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
        if relay_error is not None:
            if isinstance(relay_error, (KeyboardInterrupt, SystemExit)):
                raise relay_error
            raise RuntimeError(
                f"source database stream relay failed rc={rc}: {_safe_runtime_detail(relay_error)}"
                + (f"; {err}" if err else "")
            ) from relay_error
        if rc != 0:
            raise RuntimeError(f"source database stream failed rc={rc}: {err}")
        return rc


def _mysql_generated_date_format_replacement(column: str, fmt: str) -> str | None:
    col = "`" + str(column).replace("`", "``") + "`"
    if fmt == "%Y-%m-%d %H:00":
        expr = f"timestamp(date({col}), maketime(hour({col}),0,0))"
    elif fmt == "%Y-%m-%d":
        expr = f"date({col})"
    elif fmt == "%Y %U":
        expr = f"concat(year({col}),' ',lpad(week({col},0),2,'0'))"
    elif fmt == "%Y-%m":
        expr = f"concat(year({col}),'-',lpad(month({col}),2,'0'))"
    elif fmt == "%Y":
        expr = f"year({col})"
    else:
        return None
    return f"GENERATED ALWAYS AS ({expr})"


def _rewrite_mysql_generated_date_format_sql_line(line: str) -> str:
    def repl(match: re.Match[str]) -> str:
        replacement = _mysql_generated_date_format_replacement(match.group("column"), match.group("fmt"))
        return replacement or match.group(0)

    return _MYSQL_DATE_FORMAT_GENERATED_RE.sub(repl, line)


def _iter_mysql_generated_date_format_compat_sql(lines: Any) -> Any:
    def rewritten_lines() -> Any:
        for raw in lines:
            if isinstance(raw, bytes):
                text = raw.decode("utf-8", errors="surrogateescape")
            else:
                text = str(raw)
            yield _rewrite_mysql_generated_date_format_sql_line(text)

    for rewritten in iter_mysql_import_sql(rewritten_lines()):
        yield rewritten.encode("utf-8", errors="surrogateescape")


def import_target_db_stream(*, target_db_name: str, target_db_user: str, target_db_password: str, input_stream: Any) -> dict[str, Any]:
    target_db = _target_db_from_direct_values(name=target_db_name, user=target_db_user, password=target_db_password)
    proc: subprocess.Popen[Any] | None = None
    target_prepare_started = False
    stream_id = ""
    try:
        with gzip.GzipFile(fileobj=input_stream, mode="rb") as gz:
            stream_id = _stream_begin_id(gz.readline())
            # The stream is intentionally imported while it is relayed.  The
            # target preflight guarantees this is a new migration DB; if the
            # source/relay fails later, the exact DB is dropped below so a
            # partial import cannot be mistaken for a prepared target.
            target_prepare_started = True
            _prepare_target_db(target_db)
            proc = subprocess.Popen(
                [*_mysql_admin_base(), target_db.name],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert proc.stdin is not None
            try:
                for chunk in _iter_mysql_generated_date_format_compat_sql(
                    _iter_verified_db_stream_payload(gz, stream_id=stream_id)
                ):
                    view = memoryview(chunk)
                    while view:
                        written = proc.stdin.write(view)
                        if not written:
                            raise BrokenPipeError("target mysql stdin closed during database relay")
                        view = view[written:]
                proc.stdin.close()
                proc.stdin = None
                out_b, err_b = proc.communicate()
            except BaseException:
                if proc.stdin is not None:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                    proc.stdin = None
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    out_b, err_b = proc.communicate()
                except Exception:
                    out_b, err_b = b"", b""
                detail = _safe_runtime_detail(((out_b or b"") + (err_b or b"")).decode("utf-8", errors="replace"))
                if detail:
                    raise RuntimeError("target mysql consumer failed: " + detail)
                raise

        if proc.returncode != 0:
            out = ((out_b or b"") + (err_b or b"")).decode("utf-8", errors="replace").strip()
            raise RuntimeError("target database import failed: " + _safe_runtime_detail(out))
    except BaseException as exc:
        cleanup_error: Exception | None = None
        if target_prepare_started:
            try:
                _mysql_exec(f"DROP DATABASE IF EXISTS {_quote_ident(target_db.name)}")
            except Exception as cleanup_exc:
                cleanup_error = cleanup_exc
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if cleanup_error is not None:
            raise RuntimeError(
                "target database import failed and cleanup failed: "
                + _safe_runtime_detail(exc)
                + "; cleanup="
                + _safe_runtime_detail(cleanup_error)
            ) from exc
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("target database import failed: " + _safe_runtime_detail(exc)) from exc

    return {
        "schema": "mcd-instance-migration-target-db-v1",
        "ok": True,
        "target_db_name": target_db.name,
        "target_db_user": target_db.user,
        "stream_id": stream_id,
    }


def _assert_migration_target_contract(
    target: Path,
    *,
    domains: list[str],
    instance_uid: str,
    instance_code: str,
    phase: str | None = None,
) -> dict[str, Any]:
    expected_uid = validate_mcc_instance_uid(instance_uid)
    expected_code = validate_instance_code(instance_code)
    marker = _read_migration_target_marker(target)
    if str(marker.get("state") or "") != "in_progress":
        raise RuntimeError("migration target is not in progress")
    if phase is not None and str(marker.get("phase") or "") != phase:
        raise RuntimeError(f"migration target is not in {phase} phase")
    marker_domains = [_clean_domain(str(value or "")) for value in list(marker.get("domains") or [])]
    if marker_domains != domains:
        raise RuntimeError("migration target domains do not match request")
    if str(marker.get("instance_uid") or "") != expected_uid:
        raise RuntimeError("migration target UID does not match request")
    if str(marker.get("instance_code") or "") != expected_code:
        raise RuntimeError("migration target code does not match request")
    return marker


@stable_source_operation
def finalize_target_relay(
    config: AgentConfig,
    *,
    target_root: str,
    target_db_name: str,
    target_db_user: str,
    target_db_password: str,
    domains_json: str,
    php_version: str | None = None,
    runtime_adapter: str | None = None,
    runtime: str | None = None,
    image_ref: str | None = None,
    install_type: str | None = None,
    instance_uid: str | None = None,
    instance_code: str | None = None,
) -> dict[str, Any]:
    target = Path(target_root).resolve()
    domains = _json_domains(domains_json)
    if not instance_uid or not instance_code:
        raise RuntimeError("instance_uid and instance_code are required from MCC")
    validate_mcc_instance_uid(instance_uid)
    validate_instance_code(instance_code)
    _assert_migration_target_contract(
        target,
        domains=domains,
        instance_uid=instance_uid,
        instance_code=instance_code,
    )
    identity_names = instance_path_names(domains[0], instance_code) if domains else {}
    target_db_name = str(target_db_name or identity_names.get("db_name") or "")
    target_db_user = str(target_db_user or identity_names.get("db_user") or "")
    target_db = _target_db_from_direct_values(name=target_db_name, user=target_db_user, password=target_db_password)
    if runtime_adapter:
        result = run_runtime_adapter(
            runtime_adapter,
            operation="target-finalize",
            payload={
                "target_root": str(target),
                "target_db_name": target_db.name,
                "target_db_user": target_db.user,
                "target_db_password": target_db.password,
                "domains": domains,
                "runtime": str(runtime or "host").strip().lower() or "host",
                "image_ref": str(image_ref or ""),
                "install_type": str(install_type or "unknown"),
                "instance_uid": str(instance_uid or ""),
                "instance_code": str(instance_code or ""),
                "tls_mode": "post_cutover_certbot",
                "source_certificate_allowed": False,
            },
            timeout_sec=1800,
        )
        if bool(result.get("ok")) and bool(result.get("catchup_ok", True)):
            if instance_uid and instance_code:
                write_instance_identity(
                    target,
                    instance_uid=instance_uid,
                    instance_code=instance_code,
                    primary_domain=domains[0] if domains else None,
                )
            _mark_migration_target_prepared(target)
            result.setdefault("instance_uid", str(instance_uid or ""))
            result.setdefault("instance_code", str(instance_code or ""))
            result.setdefault(
                "identity",
                identity_readback(
                    target,
                    instance_uid=instance_uid,
                    instance_code=instance_code,
                    primary_domain=domains[0] if domains else None,
                ),
            )
            result.update(
                {
                    "phase": "target_prepared",
                    "migration_complete": False,
                    "dns_cutover_ready": True,
                    "tls_pending": True,
                    "source_certificate_used": False,
                    "next_operation": "target-issue-certificate",
                }
            )
        return result
    print("progress: 88 target web config")
    _patch_local_php_db(target, target_db)
    _patch_local_php_instance_paths(target)
    site = _write_nginx_vhost(root=target, domains=domains, php_version=php_version or "")
    _run_checked(["chown", "-R", "www-data:www-data", str(target)], timeout_sec=1800)
    print("progress: 94 target healthcheck")
    health = _target_healthcheck(target)
    if instance_uid and instance_code:
        write_instance_identity(
            target,
            instance_uid=instance_uid,
            instance_code=instance_code,
            primary_domain=domains[0] if domains else None,
        )
    inv = InstanceInventory(config.state_db_path)
    instances = inv.rescan(config)
    _mark_migration_target_prepared(target)
    return {
        "schema": "mcd-instance-migration-result-v2",
        "ok": True,
        "catchup_ok": True,
        "phase": "target_prepared",
        "migration_complete": False,
        "dns_cutover_ready": True,
        "tls_pending": True,
        "source_certificate_used": False,
        "next_operation": "target-issue-certificate",
        "completed_at_utc": _utc_now(),
        "target_root": str(target),
        "domains": domains,
        "target_db_name": target_db.name,
        "target_db_user": target_db.user,
        "instance_uid": str(instance_uid or ""),
        "instance_code": str(instance_code or ""),
        "identity": identity_readback(
            target,
            instance_uid=instance_uid,
            instance_code=instance_code,
            primary_domain=domains[0] if domains else None,
        ),
        "nginx_vhost": site,
        "healthcheck_tail": health,
        "instances": instances,
    }


def _prepared_target_identity(
    target: Path,
    *,
    domains: list[str],
    instance_uid: str,
    instance_code: str,
) -> dict[str, Any]:
    expected_uid = validate_mcc_instance_uid(instance_uid)
    expected_code = validate_instance_code(instance_code)
    _assert_migration_target_contract(
        target,
        domains=domains,
        instance_uid=expected_uid,
        instance_code=expected_code,
        phase="target_prepared",
    )
    identity = identity_readback(
        target,
        instance_uid=expected_uid,
        instance_code=expected_code,
        primary_domain=domains[0] if domains else None,
    )
    return identity


@stable_source_operation
def issue_target_certificate(
    config: AgentConfig,
    *,
    target_root: str,
    domains_json: str,
    instance_uid: str,
    instance_code: str,
    php_version: str | None = None,
) -> dict[str, Any]:
    target = Path(target_root).resolve()
    receipt: dict[str, Any] = {
        "schema": "mcd-instance-migration-certificate-receipt-v1",
        "ok": False,
        "phase": "certificate_pending",
        "migration_complete": False,
        "target_root": str(target),
        "domains": [],
        "instance_uid": str(instance_uid or ""),
        "instance_code": str(instance_code or ""),
        "identity": {},
        "source_certificate_used": False,
        "rollback_required": True,
        "rollback_contract": {
            "dns": "restore_source_route",
            "source": "instance-migrate source-restore",
            "target": "cleanup_only_after_dns_rollback",
        },
    }
    stage = "preflight"
    preflight_complete = False
    try:
        domains = _json_domains(domains_json)
        if not domains:
            raise RuntimeError("target certificate requires at least one domain")
        uid = validate_mcc_instance_uid(instance_uid)
        code = validate_instance_code(instance_code)
        receipt.update(
            {
                "domains": domains,
                "instance_uid": uid,
                "instance_code": code,
            }
        )
        identity = _prepared_target_identity(
            target,
            domains=domains,
            instance_uid=uid,
            instance_code=code,
        )
        primary = domains[0]
        plan = ImageInstallPlan(
            image_ref="instance-migration",
            domain=primary,
            short_name=primary.split(".", 1)[0],
            db_name="migration",
            db_user="migration",
            db_password="",
            webroot=target,
            php_version=_detect_php_version(php_version or ""),
            instance_uid=uid,
            instance_code=code,
        )
        receipt.update(
            {
                "identity": identity,
            }
        )
        preflight_complete = True
        stage = "certbot"
        certbot_args = [
            "certbot",
            "--nginx",
            "--cert-name",
            primary,
            "--non-interactive",
            "--agree-tos",
            "--redirect",
            "--keep-until-expiring",
            "--register-unsafely-without-email",
        ]
        for domain in domains:
            certbot_args.extend(["-d", domain])
        rc, out = _run_certbot(certbot_args, timeout_sec=900)
        if rc != 0:
            raise RuntimeError(f"certbot failed rc={rc}: {_safe_runtime_detail(out)}")

        stage = "certificate_verification"
        cert_path = LETSENCRYPT_ROOT / "live" / primary / "fullchain.pem"
        certificate = _load_pem_certificate_identity(cert_path, domain=primary)
        covered = {str(value or "").strip().lower().rstrip(".") for value in list(certificate.get("dns_names") or [])}
        missing_domains = [domain for domain in domains if domain not in covered]
        if missing_domains:
            raise RuntimeError("issued certificate does not cover: " + ",".join(missing_domains))

        stage = "https_verification"
        verification = _verify_image_https(plan)
        InstanceInventory(config.state_db_path).rescan(config)
        completed_at_utc = _utc_now()
        instances = [
            {
                **identity,
                "domains": domains,
                "observed_at_utc": completed_at_utc,
            }
        ]
        _finish_migration_target(target)
        receipt.update(
            {
                "ok": True,
                "phase": "target_tls_verified",
                "migration_complete": False,
                "target_ready": True,
                "tls_pending": False,
                "rollback_required": False,
                "rollback_contract": {},
                "next_operation": "source-finalize",
                "completed_at_utc": completed_at_utc,
                "certificate": certificate,
                "verification": verification,
                "instances": instances,
            }
        )
        return receipt
    except Exception as exc:
        if preflight_complete:
            _mark_migration_target_failed(target, f"{stage}: {exc}")
        receipt.update(
            {
                "phase": "certificate_failed",
                "error_code": f"{stage}_failed",
                "error": _safe_runtime_detail(exc),
                "failed_at_utc": _utc_now(),
            }
        )
        return receipt


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


def _php_fpm_socket(php_version: str) -> Path:
    return Path(f"/run/php/php{php_version}-fpm.sock")


def _nginx_web_root(root: Path) -> Path:
    for rel in ("docroot", "public"):
        candidate = root / rel
        if (candidate / "index.php").exists():
            return candidate
    return root


def _ensure_nginx_sites_layout() -> None:
    baseline = ensure_nginx_baseline(reload_service=False)
    if str(baseline.get("status") or "").strip().lower() == "error":
        raise RuntimeError("nginx baseline failed: " + str(baseline.get("error") or baseline))
    for path in (NGINX_SITES_AVAILABLE, NGINX_SITES_ENABLED):
        if path.exists() and not path.is_dir():
            raise RuntimeError(f"nginx sites path is not a directory: {path}")
        path.mkdir(parents=True, exist_ok=True)


def _write_nginx_vhost(*, root: Path, domains: list[str], php_version: str) -> str:
    if not domains:
        raise RuntimeError("at least one domain is required for nginx vhost")
    _ensure_nginx_sites_layout()
    php = _detect_php_version(php_version)
    if not php:
        raise RuntimeError("target PHP-FPM version is not available")
    sock = _php_fpm_socket(php)
    if not sock.exists():
        raise RuntimeError(f"target PHP-FPM socket is missing: {sock}")
    primary = domains[0]
    web_root = _nginx_web_root(root)
    site = NGINX_SITES_AVAILABLE / f"{primary}.conf"
    enabled = NGINX_SITES_ENABLED / f"{primary}.conf"
    server_names = " ".join(domains)
    content = render_nginx_template(
        "instance_migrate_vhost.conf",
        LISTEN_DIRECTIVES="listen 80;",
        SERVER_NAMES=server_names,
        WEB_ROOT=web_root,
        SSL_BLOCK="",
        FASTCGI_SOCKET=sock,
        FORM_EMBED_LOCATION="\n".join(
            ("    " + line) if line else ""
            for line in render_form_embed_location(fastcgi_pass=f"unix:{sock}").splitlines()
        ),
    )
    content = ensure_mautic_public_app_asset_locations(content)
    content = normalize_legacy_http2_listen(content, modern_http2=_nginx_supports_http2_directive())
    site.write_text(content, encoding="utf-8")
    enabled.parent.mkdir(parents=True, exist_ok=True)
    if enabled.exists() or enabled.is_symlink():
        enabled.unlink()
    enabled.symlink_to(site)
    _run_checked(["nginx", "-t"], timeout_sec=30)
    _run_checked(["systemctl", "reload", "nginx"], timeout_sec=30)
    return str(site)


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


@stable_source_operation
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
    instance_uid: str | None = None,
    instance_code: str | None = None,
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
    domains = _json_domains(domains_json)
    if not instance_uid or not instance_code:
        raise RuntimeError("instance_uid and instance_code are required from MCC")
    validate_mcc_instance_uid(instance_uid)
    validate_instance_code(instance_code)
    if instance_code:
        identity_names = instance_path_names(domains[0], instance_code) if domains else {}
        target_db_name = str(target_db_name or identity_names.get("db_name") or "") or None
        target_db_user = str(target_db_user or identity_names.get("db_user") or "") or None
    target_archive = _prepare_migration_target(
        target,
        domains=domains,
        claim=True,
        source_root=source_root_clean,
        instance_uid=instance_uid,
        instance_code=instance_code,
    ) if _path_is_below(target, MIGRATION_ROOT) else None
    if not _path_is_below(target, MIGRATION_ROOT) and target.exists() and any(target.iterdir()):
        raise RuntimeError(f"target root already exists and is not empty: {target}")

    started = _utc_now()
    result: dict[str, Any] = {
        "schema": "mcd-instance-migration-result-v1",
        "ok": False,
        "catchup_ok": False,
        "started_at_utc": started,
        "target_root": str(target),
        "target_archive_root": str(target_archive) if target_archive is not None else "",
        "domains": domains,
        "target_db_name": str(target_db_name or ""),
        "target_db_user": str(target_db_user or ""),
        "instance_uid": str(instance_uid or ""),
        "instance_code": str(instance_code or ""),
        "identity": identity_readback(
            target,
            instance_uid=instance_uid,
            instance_code=instance_code,
            primary_domain=domains[0] if domains else None,
        ),
        "steps": [],
    }

    exclusions = [
        "/var/cache/***",
        "/var/logs/***",
        "/var/tmp/***",
        "/app/cache/***",
        "/app/logs/***",
        MIGRATION_TARGET_MARKER,
    ]
    source_quiescent = False
    source_quiescence_receipt: dict[str, Any] = {}
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

        print("progress: 60 source static maintenance and quiescence")
        source_quiescence_raw = _remote_mcd(
            source_ssh_user=source_ssh_user,
            source_address=source_address,
            source_ssh_port=source_ssh_port,
            source_ssh_key_file=source_ssh_key_file,
            args=[
                "instance-migrate",
                "source-quiesce",
                "--root",
                source_root_clean,
                "--domains-json",
                json.dumps(domains, ensure_ascii=True, separators=(",", ":")),
                "--instance-uid",
                str(instance_uid),
                "--instance-code",
                str(instance_code),
                "--json",
            ],
            timeout_sec=360,
        )
        try:
            source_quiescence_receipt = json.loads(source_quiescence_raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("source quiescence receipt is not valid JSON") from exc
        if not bool(source_quiescence_receipt.get("ok", False)) or not bool(
            source_quiescence_receipt.get("source_quiescent", False)
        ):
            raise RuntimeError("source quiescence receipt does not prove a quiescent source")
        source_quiescent = True

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
        if instance_uid and instance_code:
            write_instance_identity(
                target,
                instance_uid=instance_uid,
                instance_code=instance_code,
                primary_domain=domains[0] if domains else None,
            )
        source_db_port = int(copied_source_db.port or 3306)

        print("progress: 72 source DB tunnel")
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

        print("progress: 76 database import")
        result["steps"].append(_dump_source_into_target(config, local_source_db=source_db, target_db=target_db, label="single"))
        _patch_local_php_db(target, target_db)
        patched_paths = _patch_local_php_instance_paths(target)
        if patched_paths:
            result["steps"].append({"label": "local_paths", "keys": patched_paths})

        print("progress: 88 target web config")
        site = _write_nginx_vhost(root=target, domains=domains, php_version=php_version or "")
        _run_checked(["chown", "-R", "www-data:www-data", str(target)], timeout_sec=1800)

        print("progress: 94 target healthcheck")
        health = _target_healthcheck(target)
        inv = InstanceInventory(config.state_db_path)
        instances = inv.rescan(config)

        _mark_migration_target_prepared(target)

        result.update(
            {
                "ok": True,
                "catchup_ok": True,
                "phase": "target_prepared",
                "migration_complete": False,
                "dns_cutover_ready": True,
                "tls_pending": True,
                "source_certificate_used": False,
                "source_instance_state_changed": False,
                "next_operation": "target-issue-certificate",
                "completed_at_utc": _utc_now(),
                "source_maintenance_active": True,
                "source_maintenance_restored": False,
                "source_quiescent": True,
                "source_quiescence_receipt": source_quiescence_receipt,
                "nginx_vhost": site,
                "healthcheck_tail": health,
                "instances": instances,
                "identity": identity_readback(
                    target,
                    instance_uid=instance_uid,
                    instance_code=instance_code,
                    primary_domain=domains[0] if domains else None,
                ),
            }
        )
        print("progress: 100 target prepared; DNS cutover and target certificate pending")
        return result
    except Exception as exc:
        _mark_migration_target_failed(target, str(exc))
        if source_quiescent:
            try:
                restore_raw = _remote_mcd(
                    source_ssh_user=source_ssh_user,
                    source_address=source_address,
                    source_ssh_port=source_ssh_port,
                    source_ssh_key_file=source_ssh_key_file,
                    args=[
                        "instance-migrate",
                        "source-restore",
                        "--root",
                        source_root_clean,
                        "--domains-json",
                        json.dumps(domains, ensure_ascii=True, separators=(",", ":")),
                        "--instance-uid",
                        str(instance_uid),
                        "--instance-code",
                        str(instance_code),
                        "--json",
                    ],
                    timeout_sec=180,
                )
                restore_receipt = json.loads(restore_raw)
                if not bool(restore_receipt.get("ok", False)):
                    raise RuntimeError("source restore receipt is not successful")
                result["source_maintenance_restored"] = True
                result["source_restore_receipt"] = restore_receipt
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
        "user": inst.db.user,
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


def _select_instance(instances: list[MauticInstall], selector: str | None, *, host_name: str = "") -> MauticInstall:
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
        if (token in candidates or token_l in {x.lower() for x in candidates if x}
                or instance_uid_matches(token, str(inst.instance_uid or ""), host_name)):
            return inst
    raise RuntimeError(f"instance not found: {token}")


def collect_source_probe(config: AgentConfig, selector: str | None = None) -> dict[str, Any]:
    inv = InstanceInventory(config.state_db_path)
    ensure_seeded(inv, config)
    instances = inv.list_instances()
    inst = _select_instance(instances, selector, host_name=_mcc_wire_host_name(config))
    root = Path(inst.root).resolve()
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"instance root is not a directory: {root}")

    files_bytes = _du_bytes(root)
    db_payload = _database_probe(inst)
    db_bytes = _safe_int(db_payload.get("size_bytes"), 0)
    descriptor = descriptor_for_root(str(root))
    runtime = str(inst.runtime or "host").strip().lower() or "host"
    install_type = str(inst.install_type or "unknown").strip().lower() or "unknown"
    profile = build_runtime_profile(
        runtime=runtime,
        install_type=install_type,
        capabilities=inst.runtime_capabilities,
    )
    version_evidence = read_mautic_version_evidence_read_only(str(root))
    payload: dict[str, Any] = {
        "schema": "mcd-instance-migration-source-probe-v1",
        "ok": True,
        "checked_at_utc": _utc_now(),
        "instance": {
            "instance_uid": inst.instance_uid,
            "instance_code": str(getattr(inst, "instance_code", None) or "") or None,
            "name": inst.name,
            "root": str(root),
            "primary_domain": inst.primary_domain or "",
            "domains": list(inst.domains or []),
            "mautic_major": inst.mautic_major,
            "mautic_version": version_evidence.get("version"),
            "mautic_version_source": version_evidence.get("source"),
            "source": inst.source,
            "runtime": runtime,
            "install_type": install_type,
            "runtime_profile": profile.safe_dict(),
            "runtime_adapter": descriptor.migration_adapter if descriptor is not None else "",
            "image_ref": str(inst.runtime_image_ref or ""),
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
