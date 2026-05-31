from __future__ import annotations

import hashlib
import json
import ipaddress
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from mcd_agent import __version__
from mcd_agent.amazon_mailer_dep import ensure_mailer_packages_for_bundles
from mcd_agent.cluster_routing import cluster_local_identity_values, cluster_route_targets
from mcd_agent.config import AgentConfig
from mcd_agent.db import MauticDB
from mcd_agent.discovery import discover_mautic
from mcd_agent.executor import build_mautic_exec_args, execute_mautic_command_template
from mcd_agent.host_identity import resolve_agent_identity
from mcd_agent.runtime_overrides import fetch_runtime_overrides
from mcd_agent.state_backend import mysql_state_enabled, mysql_state_existing_connection, mysql_state_table_names


_C_RESET = "\033[0m"
_C_GREEN = "\033[32m"
_C_YELLOW = "\033[33m"
_C_RED = "\033[31m"
_C_GRAY = "\033[90m"
_BUNDLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*Bundle(?:Dev)?$")
_PLUGIN_UID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{2,127}$")
_CMD_SEP = "\x1f"
_CLUSTER_PLUGIN_SYNC_PREFIX = "mcd_plugin_operation:"
_CLUSTER_PLUGIN_SYNC_HOST_PREFIX = "__cluster__:"
_CLUSTER_PLUGIN_ACTIVE_PHASES = {
    "files",
    "files_running",
    "files_done",
    "file_sync",
    "cache_clear",
    "plugin_install",
}
_CLUSTER_PLUGIN_STALE_SEC = 3 * 60 * 60
_PLUGIN_SYNC_IGNORED_NAMES = {".DS_Store", "__MACOSX", ".stfolder", ".stversions"}
_PLUGIN_SYNC_IGNORED_RE = re.compile(r"(sync-conflict|\.sync-conflict|\.syncthing\..*\.tmp$|\.tmp$|\.part$)", re.IGNORECASE)
_GALERA_DANGEROUS_SQL_RE = re.compile(
    r"^\s*(ALTER|CREATE|DROP|RENAME|TRUNCATE|OPTIMIZE|ANALYZE|CHECK|REPAIR|LOCK|UNLOCK)\b",
    re.IGNORECASE,
)
_EXCLUSIVE_BUNDLE_PAIRS: dict[str, str] = {
    "AmazonSesBundle": "AmazonSesBundleDev",
    "AmazonSesBundleDev": "AmazonSesBundle",
    "MauticAmazonSesBundle": "AmazonSnsCallbackBundle",
    "AmazonSnsCallbackBundle": "MauticAmazonSesBundle",
    "SalesSnapBundle": "SalesSnapBundleDev",
    "SalesSnapBundleDev": "SalesSnapBundle",
}

_EXCLUSIVE_BUNDLE_GROUPS: tuple[set[str], ...] = (
    {
        "AmazonSesBundle",
        "MauticAmazonSesBundle",
        "AmazonSnsCallbackBundle",
    },
)


def _is_valid_bundle_name(name: str) -> bool:
    n = name.strip()
    if not n:
        return False
    if n.lower() in {"plugin", "plugins"}:
        return False
    return bool(_BUNDLE_NAME_RE.match(n))


def _normalize_plugin_uid(uid: str) -> str:
    raw = str(uid or "").strip().lower()
    raw = re.sub(r"[^a-z0-9_.:-]+", "-", raw)
    raw = raw.strip("-._:")
    return raw[:128]


def _valid_plugin_uid(uid: str) -> bool:
    return bool(_PLUGIN_UID_RE.match(str(uid or "").strip()))


def _install_bundle_for_manifest_bundle(bundle: str, item: dict[str, Any] | None = None) -> str:
    """
    Resolve filesystem install directory for a manifest bundle key.
    Manifest may explicitly override the install directory with
    `install_bundle`. Otherwise legacy Dev aliases are installed into the
    canonical bundle directory so runtime bundle paths remain stable.
    """
    b = str(bundle or "").strip()
    if isinstance(item, dict):
        explicit = str(item.get("install_bundle", "")).strip()
        if _is_valid_bundle_name(explicit):
            return explicit
    if b in _EXCLUSIVE_BUNDLE_PAIRS and b.endswith("Dev"):
        return _EXCLUSIVE_BUNDLE_PAIRS[b]
    return b


def _color(status: str, no_color: bool) -> str:
    if no_color:
        return status
    if status == "OK":
        return f"{_C_GREEN}{status}{_C_RESET}"
    if status == "UPDATE":
        return f"{_C_YELLOW}{status}{_C_RESET}"
    if status == "MISSING":
        return f"{_C_GRAY}{status}{_C_RESET}"
    if status == "BROKEN":
        return f"{_C_RED}{status}{_C_RESET}"
    return status


def _status_cell(status: str, no_color: bool, width: int = 7) -> str:
    colored = _color(status, no_color)
    pad = " " * max(0, width - len(status))
    return f"{colored}{pad}"


def _select_install(config: AgentConfig, root: str | None) -> tuple[str, int]:
    installs = discover_mautic(
        config.discovery_roots,
        config.exclude_path_contains,
        config.supported_mautic_majors,
        config.custom_instances,
    )
    if root:
        for inst in installs:
            if inst.root == root:
                return inst.root, inst.mautic_major or 6
        raise RuntimeError(f"Mautic install not found for root: {root}")
    if not installs:
        raise RuntimeError("No Mautic install found")
    if len(installs) > 1:
        roots = ", ".join(x.root for x in installs)
        raise RuntimeError(f"Multiple installs found, pass --root: {roots}")
    inst = installs[0]
    return inst.root, inst.mautic_major or 6


def _select_install_with_db(config: AgentConfig, root: str | None):
    installs = discover_mautic(
        config.discovery_roots,
        config.exclude_path_contains,
        config.supported_mautic_majors,
        config.custom_instances,
    )
    if root:
        for inst in installs:
            if inst.root == root:
                return inst
        raise RuntimeError(f"Mautic install not found for root: {root}")
    if not installs:
        raise RuntimeError("No Mautic install found")
    if len(installs) > 1:
        roots = ", ".join(x.root for x in installs)
        raise RuntimeError(f"Multiple installs found, pass --root: {roots}")
    return installs[0]


def _dedupe_text(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _cluster_local_host_name(config: AgentConfig) -> str:
    try:
        idx = int(getattr(config, "cluster_node_index", 0) or 0)
    except Exception:
        idx = 0
    if idx > 0:
        hosts = cluster_route_targets(config, "cache")
        if 0 < idx <= len(hosts):
            text = str(hosts[idx - 1] or "").strip()
            if text:
                return text
    try:
        ident = resolve_agent_identity(config)
    except Exception:
        ident = {}
    for key in (
        "effective_mcc_host_name",
        "configured_host_name",
        "local_hostname",
        "effective_hostname",
    ):
        text = str(ident.get(key) or "").strip() if isinstance(ident, dict) else ""
        if text:
            return text
    return "unknown-host"


def _cluster_plugin_expected_hosts(config: AgentConfig) -> list[str]:
    hosts = list(cluster_route_targets(config, "cache"))
    ref = _cluster_plugin_reference_host(config)
    if ref:
        hosts.insert(0, ref)
    local = _cluster_local_host_name(config)
    if local:
        hosts.append(local)
    return _dedupe_text(hosts)


def _cluster_plugin_reference_host(config: AgentConfig) -> str:
    hosts = cluster_route_targets(config, "cache")
    if hosts:
        return str(hosts[0] or "").strip()
    return _cluster_local_host_name(config)


def _cluster_plugin_required(config: AgentConfig) -> bool:
    return bool(getattr(config, "cluster_id", None)) and len(_cluster_plugin_expected_hosts(config)) > 1


def _cluster_plugin_local_is_reference(config: AgentConfig) -> bool:
    ref = _cluster_plugin_reference_host(config).strip().lower()
    if not ref:
        return True
    local = {x.strip().lower() for x in cluster_local_identity_values(config) if str(x or "").strip()}
    local.add(_cluster_local_host_name(config).strip().lower())
    return ref in local


def _cluster_plugin_host_is_local(config: AgentConfig, host: str) -> bool:
    target = str(host or "").strip().lower()
    if not target:
        return False
    local = {x.strip().lower() for x in cluster_local_identity_values(config) if str(x or "").strip()}
    local.add(_cluster_local_host_name(config).strip().lower())
    return target in local


def _cluster_plugin_sync_key(config: AgentConfig, root: str) -> str:
    cluster_id = str(getattr(config, "cluster_id", "") or "default").strip() or "default"
    root_hash = hashlib.sha256(f"{cluster_id}\0{root}".encode("utf-8")).hexdigest()[:24]
    return f"{_CLUSTER_PLUGIN_SYNC_PREFIX}{root_hash}"


def _cluster_plugin_sync_host(config: AgentConfig) -> str:
    cluster_id = str(getattr(config, "cluster_id", "") or "default").strip() or "default"
    return f"{_CLUSTER_PLUGIN_SYNC_HOST_PREFIX}{cluster_id}"


def _cluster_plugin_row_signature(action: str, selected: list[dict[str, Any]], auto_remove_bundles: list[str]) -> str:
    rows: list[dict[str, str]] = []
    for row in selected:
        item = row.get("item")
        item_dict = item if isinstance(item, dict) else {}
        bundle = str(row.get("bundle", "") or "").strip()
        rows.append(
            {
                "bundle": bundle,
                "install_bundle": str(row.get("install_bundle") or _install_bundle_for_manifest_bundle(bundle, item_dict)).strip(),
                "plugin_uid": str(row.get("plugin_uid", "") or "").strip(),
                "version": str((item_dict or {}).get("version", "") or row.get("server_version", "") or "").strip(),
                "sha256": str((item_dict or {}).get("sha256", "") or "").strip(),
            }
        )
    payload = {
        "action": str(action or "").strip(),
        "rows": sorted(rows, key=lambda x: (x.get("plugin_uid") or "", x.get("bundle") or "")),
        "auto_remove": sorted(str(x).strip() for x in auto_remove_bundles if str(x).strip()),
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _split_sql_statements(sql: str) -> list[str]:
    text = str(sql or "")
    parts: list[str] = []
    start = 0
    quote = ""
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch in {"'", '"', "`"}:
            quote = ch
        elif ch == ";":
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return [x for x in parts if str(x or "").strip()]


def _strip_sql_leading_comments(statement: str) -> str:
    text = str(statement or "").lstrip()
    while text:
        if text.startswith("--") or text.startswith("#"):
            nl = text.find("\n")
            if nl < 0:
                return ""
            text = text[nl + 1 :].lstrip()
            continue
        if text.startswith("/*!"):
            end = text.find("*/")
            inner = text[3:end if end >= 0 else len(text)]
            inner = re.sub(r"^\d+\s*", "", inner).lstrip()
            suffix = text[end + 2 :] if end >= 0 else ""
            return f"{inner} {suffix}".lstrip()
        if text.startswith("/*"):
            end = text.find("*/")
            if end < 0:
                return ""
            text = text[end + 2 :].lstrip()
            continue
        break
    return text


def _cluster_pre_sql_is_dangerous(sql: str) -> bool:
    for statement in _split_sql_statements(sql):
        head = _strip_sql_leading_comments(statement)
        if head and _GALERA_DANGEROUS_SQL_RE.search(head):
            return True
    return False


def _cluster_plugin_mutate(config: AgentConfig, key: str, mutator: Any, *, host_name: str | None = None) -> Any:
    names = mysql_state_table_names(config)
    table = names["runtime_sync"]
    runtime_host = str(host_name or _cluster_plugin_sync_host(config)).strip()
    now_s = int(time.time())
    conn = mysql_state_existing_connection(config)
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO `{table}`(host_name, `key`, payload_json, updated_at)
                VALUES(%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE updated_at=updated_at
                """,
                (runtime_host, key, "{}", now_s),
            )
            cur.execute(
                f"""
                SELECT payload_json
                FROM `{table}`
                WHERE host_name=%s AND `key`=%s
                FOR UPDATE
                """,
                (runtime_host, key),
            )
            row = cur.fetchone() or {}
            try:
                payload = json.loads(str(row.get("payload_json") or "{}")) if isinstance(row, dict) else {}
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            result = mutator(payload)
            payload["updated_at"] = now_s
            cur.execute(
                f"""
                UPDATE `{table}`
                SET payload_json=%s, updated_at=%s
                WHERE host_name=%s AND `key`=%s
                """,
                (json.dumps(payload, ensure_ascii=True, separators=(",", ":")), now_s, runtime_host, key),
            )
        conn.commit()
        return result
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _cluster_plugin_read_payload(config: AgentConfig, key: str, *, host_name: str | None = None) -> dict[str, Any]:
    names = mysql_state_table_names(config)
    table = names["runtime_sync"]
    runtime_host = str(host_name or _cluster_plugin_sync_host(config)).strip()
    conn = mysql_state_existing_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT payload_json FROM `{table}` WHERE host_name=%s AND `key`=%s LIMIT 1",
                (runtime_host, key),
            )
            row = cur.fetchone()
        if not isinstance(row, dict):
            return {}
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _cluster_plugin_set_phase(
    config: AgentConfig,
    key: str,
    *,
    phase: str,
    local_host: str,
    request_hash: str,
    message: str = "",
    node_status: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    now_s = int(time.time())

    def mutator(payload: dict[str, Any]) -> None:
        payload["phase"] = str(phase)
        payload["request_hash"] = str(request_hash)
        payload["message"] = str(message)[:500]
        if extra:
            payload.update(dict(extra))
        nodes = payload.setdefault("nodes", {})
        if not isinstance(nodes, dict):
            nodes = {}
            payload["nodes"] = nodes
        node = nodes.setdefault(local_host, {})
        if isinstance(node, dict):
            node["last_seen"] = now_s
            if node_status:
                node["status"] = node_status
            if message:
                node["message"] = str(message)[:500]

    _cluster_plugin_mutate(config, key, mutator)


def _cluster_plugin_begin_reference(
    config: AgentConfig,
    key: str,
    *,
    invocation_id: str,
    request_hash: str,
    root: str,
    action: str,
    expected_hosts: list[str],
    reference_host: str,
    local_host: str,
) -> dict[str, Any]:
    now_s = int(time.time())

    def mutator(payload: dict[str, Any]) -> dict[str, Any]:
        phase = str(payload.get("phase", "") or "").strip()
        updated = int(payload.get("updated_at") or 0)
        active = phase in _CLUSTER_PLUGIN_ACTIVE_PHASES and (now_s - updated) <= _CLUSTER_PLUGIN_STALE_SEC
        existing_hash = str(payload.get("request_hash", "") or "")
        if active:
            if existing_hash == request_hash:
                return {
                    "action": "wait",
                    "message": f"cluster plugin operation already active phase={phase}",
                }
            return {
                "action": "busy",
                "message": f"another cluster plugin operation is active phase={phase}",
            }
        payload.clear()
        payload.update(
            {
                "cluster_id": str(getattr(config, "cluster_id", "") or ""),
                "phase": "files",
                "request_hash": request_hash,
                "invocation_id": invocation_id,
                "root": root,
                "action_name": action,
                "reference_host": reference_host,
                "expected_hosts": expected_hosts,
                "created_at": now_s,
                "nodes": {
                    local_host: {
                        "status": "reference_started",
                        "last_seen": now_s,
                    }
                },
            }
        )
        return {"action": "execute", "message": "reference operation started"}

    return _cluster_plugin_mutate(config, key, mutator)


def _repo_base_url(config: AgentConfig) -> str:
    if config.plugins_repo_base_url:
        return config.plugins_repo_base_url.rstrip("/")
    if config.mcc_url:
        return config.mcc_url.rstrip("/")
    raise RuntimeError("plugins.repo_base_url or mcc.url must be configured")


_DNS_OVERRIDE_LOCK = threading.Lock()


def _is_ip_literal(host: str | None) -> bool:
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _url_host(url: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    return parsed.hostname


def _plugins_fallback_ip(config: AgentConfig) -> str | None:
    local = str(config.plugins_repo_fallback_ip or "").strip()
    if local:
        return local
    if not (config.mcc_url and config.mcc_token):
        return None
    try:
        fetched = fetch_runtime_overrides(config)
        if str(fetched.get("status", "")).strip().lower() != "ok":
            return None
        runtime = fetched.get("runtime_overrides")
        if not isinstance(runtime, dict):
            return None
        remote = str(runtime.get("plugins_repo_fallback_ip", "")).strip()
        return remote or None
    except Exception:
        return None


def _urlopen_with_dns_override(
    req: urllib.request.Request,
    *,
    timeout_sec: int,
    resolve_host: str,
    resolve_ip: str,
):
    orig_getaddrinfo = socket.getaddrinfo
    resolve_host_l = resolve_host.lower()

    def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # type: ignore[no-untyped-def]
        h = str(host or "")
        if h.lower() == resolve_host_l:
            h = resolve_ip
        return orig_getaddrinfo(h, port, family, type, proto, flags)

    with _DNS_OVERRIDE_LOCK:
        socket.getaddrinfo = _patched_getaddrinfo
        try:
            return urllib.request.urlopen(req, timeout=timeout_sec)
        finally:
            socket.getaddrinfo = orig_getaddrinfo


def _fetch_json(
    url: str,
    token: str | None,
    *,
    timeout_sec: int = 12,
    fallback_ip: str | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": f"mcd-agent/{__version__}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    primary_err: Exception | None = None
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = resp.read().decode("utf-8")
        return json.loads(data)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        primary_err = e

    host = _url_host(url)
    fallback_ip_clean = str(fallback_ip or "").strip()
    if not fallback_ip_clean or not host or _is_ip_literal(host):
        assert primary_err is not None
        raise primary_err

    logging.warning("plugins manifest primary fetch failed (%s), fallback via %s -> %s", primary_err, host, fallback_ip_clean)
    try:
        with _urlopen_with_dns_override(
            req,
            timeout_sec=timeout_sec,
            resolve_host=host,
            resolve_ip=fallback_ip_clean,
        ) as resp:
            data = resp.read().decode("utf-8")
        return json.loads(data)
    except (urllib.error.HTTPError, urllib.error.URLError):
        assert primary_err is not None
        raise primary_err


def _fetch_file(url: str, token: str | None, dst: Path, *, fallback_ip: str | None = None) -> None:
    headers: dict[str, str] = {"User-Agent": f"mcd-agent/{__version__}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    primary_err: Exception | None = None
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, dst.open("wb") as f:
            shutil.copyfileobj(resp, f)
        return
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        primary_err = e

    host = _url_host(url)
    fallback_ip_clean = str(fallback_ip or "").strip()
    if not fallback_ip_clean or not host or _is_ip_literal(host):
        assert primary_err is not None
        raise primary_err

    logging.warning("plugins package primary fetch failed (%s), fallback via %s -> %s", primary_err, host, fallback_ip_clean)
    try:
        with _urlopen_with_dns_override(
            req,
            timeout_sec=120,
            resolve_host=host,
            resolve_ip=fallback_ip_clean,
        ) as resp, dst.open("wb") as f:
            shutil.copyfileobj(resp, f)
    except (urllib.error.HTTPError, urllib.error.URLError):
        assert primary_err is not None
        raise primary_err


def _parse_selection(expr: str, max_index: int) -> list[int]:
    out: set[int] = set()
    parts = [x.strip() for x in expr.split() if x.strip()]
    for part in parts:
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                start = int(a)
                end = int(b)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            for idx in range(start, end + 1):
                if 1 <= idx <= max_index:
                    out.add(idx)
            continue
        try:
            idx = int(part)
        except ValueError:
            continue
        if 1 <= idx <= max_index:
            out.add(idx)
    return sorted(out)


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _normalize_action(raw: str) -> str | None:
    v = raw.strip().lower()
    if not v:
        return "auto"
    mapping = {
        "a": "auto",
        "auto": "auto",
        "i": "install",
        "install": "install",
        "u": "update",
        "update": "update",
        "r": "reinstall",
        "reinstall": "reinstall",
        "d": "remove",
        "remove": "remove",
        "x": "exit",
        "q": "exit",
        "exit": "exit",
    }
    return mapping.get(v)


def _exclusive_counterparts(bundle: str, item: dict[str, Any] | None = None) -> set[str]:
    """
    Returns bundles that are mutually exclusive with `bundle`.
    Supports hardcoded pairs and optional manifest field `replaces: []`.
    """
    out: set[str] = set()
    bundle_name = str(bundle or "").strip()
    if not bundle_name:
        return out

    direct = _EXCLUSIVE_BUNDLE_PAIRS.get(bundle_name)
    if direct:
        out.add(direct)
    for left, right in _EXCLUSIVE_BUNDLE_PAIRS.items():
        if right == bundle_name:
            out.add(left)
    for group in _EXCLUSIVE_BUNDLE_GROUPS:
        if bundle_name in group:
            out.update(group)

    if isinstance(item, dict):
        repl = item.get("replaces")
        if isinstance(repl, list):
            for x in repl:
                name = str(x or "").strip()
                if name and _is_valid_bundle_name(name):
                    out.add(name)

    out.discard(bundle_name)
    return out


def _validate_selected_exclusive_conflicts(selected: list[dict[str, Any]]) -> None:
    selected_set = {str(row.get("bundle", "")).strip() for row in selected if str(row.get("bundle", "")).strip()}
    for row in selected:
        bundle = str(row.get("bundle", "")).strip()
        item = row.get("item")
        item_dict = item if isinstance(item, dict) else None
        conflicts = sorted(selected_set.intersection(_exclusive_counterparts(bundle, item_dict)))
        if conflicts:
            raise RuntimeError(
                f"exclusive plugins selected together: {bundle} and {', '.join(conflicts)}"
            )


def _auto_remove_conflicting_installed_bundles(
    selected: list[dict[str, Any]],
    plugins_dir: Path,
) -> list[str]:
    """
    Return conflicting bundle keys that must be removed before applying `selected`.
    Manifest `replaces` aliases are removed unconditionally because they often
    exist only as stale DB rows. Broader exclusive counterparts are removed only
    when their plugin path exists, avoiding unrelated path churn for competitors
    that were never installed on this instance.
    """
    selected_set = {str(row.get("bundle", "")).strip() for row in selected if str(row.get("bundle", "")).strip()}
    remove: set[str] = set()
    for row in selected:
        bundle = str(row.get("bundle", "")).strip()
        item = row.get("item")
        item_dict = item if isinstance(item, dict) else None
        explicit_replaces: set[str] = set()
        repl = item_dict.get("replaces") if isinstance(item_dict, dict) else None
        if isinstance(repl, list):
            for token in repl:
                name = str(token or "").strip()
                if name and _is_valid_bundle_name(name):
                    explicit_replaces.add(name)
        for conflict in _exclusive_counterparts(bundle, item_dict):
            if conflict in selected_set:
                continue
            if conflict not in explicit_replaces and not ((plugins_dir / conflict).exists() or (plugins_dir / conflict).is_symlink()):
                continue
            remove.add(conflict)
    return sorted(remove, key=lambda x: x.lower())


def _protected_plugin_path_names(selected: list[dict[str, Any]]) -> set[str]:
    """
    Bundle variants may share the same runtime `install_bundle`.
    Auto-removal can delete conflicting manifest aliases, but must never delete
    the selected runtime path itself.
    """
    protected: set[str] = set()
    for row in selected:
        selected_bundle = str(row.get("bundle", "")).strip()
        item = row.get("item")
        selected_install_bundle = str(
            row.get("install_bundle")
            or _install_bundle_for_manifest_bundle(
                selected_bundle,
                item if isinstance(item, dict) else None,
            )
        ).strip()
        if selected_bundle:
            protected.add(selected_bundle)
        if selected_install_bundle:
            protected.add(selected_install_bundle)
    return protected


def _remove_plugin_path(path: Path) -> bool:
    if not (path.exists() or path.is_symlink()):
        return False
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    if path.is_dir():
        shutil.rmtree(path)
        return True
    return False


def _plugin_sync_ignore(path: Path, rel: str) -> bool:
    if any(part in _PLUGIN_SYNC_IGNORED_NAMES for part in path.parts):
        return True
    return bool(rel and rel != "." and _PLUGIN_SYNC_IGNORED_RE.search(rel))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _plugin_selection_digest(root: str, bundle_names: list[str]) -> dict[str, Any]:
    plugins_dir = _resolve_plugins_dir(root, create=False)
    bundles = _dedupe_text([str(x).strip() for x in bundle_names if str(x).strip()])
    h = hashlib.sha256()
    details: dict[str, Any] = {}
    errors: list[str] = []
    for bundle in sorted(bundles, key=lambda x: x.lower()):
        base = plugins_dir / bundle
        h.update(bundle.encode("utf-8", errors="surrogateescape"))
        h.update(b"\0")
        if not base.exists():
            h.update(b"MISSING\0")
            details[bundle] = {"exists": False, "files": 0, "dirs": 0, "bytes": 0}
            continue
        if not base.is_dir():
            h.update(b"NOTDIR\0")
            details[bundle] = {"exists": True, "status": "not_directory"}
            errors.append(f"{bundle}:not_directory")
            continue
        files = 0
        dirs = 0
        total_bytes = 0
        try:
            entries = [base]
            entries.extend(sorted(base.rglob("*"), key=lambda p: str(p.relative_to(base))))
            for entry in entries:
                rel = "." if entry == base else str(entry.relative_to(base))
                if _plugin_sync_ignore(entry, rel):
                    continue
                try:
                    st = entry.lstat()
                except Exception as e:
                    errors.append(f"{bundle}/{rel}:stat:{e}")
                    continue
                mode = st.st_mode & 0o7777
                if entry.is_symlink():
                    target = os.readlink(entry)
                    h.update(f"L\0{bundle}/{rel}\0{target}\n".encode("utf-8", errors="surrogateescape"))
                elif entry.is_dir():
                    dirs += 1
                    h.update(f"D\0{bundle}/{rel}\0{mode:o}\n".encode("utf-8", errors="surrogateescape"))
                elif entry.is_file():
                    files += 1
                    total_bytes += int(st.st_size)
                    try:
                        file_hash = _sha256_file(entry)
                    except Exception as e:
                        errors.append(f"{bundle}/{rel}:hash:{e}")
                        continue
                    h.update(f"F\0{bundle}/{rel}\0{st.st_size}\0{file_hash}\n".encode("utf-8", errors="surrogateescape"))
                else:
                    h.update(f"O\0{bundle}/{rel}\n".encode("utf-8", errors="surrogateescape"))
        except Exception as e:
            errors.append(f"{bundle}:scan:{e}")
        details[bundle] = {"exists": True, "files": files, "dirs": dirs, "bytes": total_bytes}
    return {
        "digest": h.hexdigest(),
        "bundles": bundles,
        "details": details,
        "errors": errors[:20],
        "status": "error" if errors else "ok",
    }


def _extract_version_from_php_text(text: str) -> str:
    m = re.search(r"['\"]version['\"]\s*=>\s*['\"]([^'\"]+)['\"]", text, flags=re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r"['\"]version['\"]\s*=\s*['\"]([^'\"]+)['\"]", text, flags=re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return "-"


def _read_installed_version(plugin_dir: Path) -> str:
    cfg = plugin_dir / "Config" / "config.php"
    if not cfg.exists():
        return "-"
    try:
        text = cfg.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return "-"
    return _extract_version_from_php_text(text)


def _resolve_plugins_dir(root: str, create: bool = False) -> Path:
    base = Path(root)
    candidates = [
        base / "plugins",
        base / "docroot" / "plugins",
        base / "public" / "plugins",
    ]
    for p in candidates:
        if p.exists() and p.is_dir():
            return p
    if create:
        candidates[0].mkdir(parents=True, exist_ok=True)
    return candidates[0]


def _build_plugin_rows(
    *,
    config: AgentConfig,
    install,
) -> tuple[str, str | None, str, list[dict[str, Any]]]:
    install_root = install.root
    major = install.mautic_major or 6
    base = _repo_base_url(config)
    manifest_url = base + config.plugins_manifest_path_template.format(major=major)
    fallback_ip = _plugins_fallback_ip(config)
    logging.info("plugins manifest: %s", manifest_url)
    if fallback_ip:
        logging.info("plugins manifest fallback_ip: %s", fallback_ip)
    manifest = _fetch_json(
        manifest_url,
        config.mcc_token,
        timeout_sec=12,
        fallback_ip=fallback_ip,
    )

    plugins = manifest.get("plugins", [])
    if not isinstance(plugins, list) or not plugins:
        return manifest_url, fallback_ip, install_root, []

    plugins_dir = _resolve_plugins_dir(install_root, create=True)
    local_dirs = (
        sorted(
            [
                x
                for x in plugins_dir.iterdir()
                if x.is_dir() and not x.name.startswith(".") and _is_valid_bundle_name(x.name)
            ],
            key=lambda p: p.name.lower(),
        )
        if plugins_dir.exists()
        else []
    )

    manifest_by_bundle: dict[str, dict[str, Any]] = {}
    for item in plugins:
        if not isinstance(item, dict):
            continue
        bundle = str(item.get("bundle", "")).strip()
        if bundle and _is_valid_bundle_name(bundle):
            manifest_by_bundle[bundle] = item

    local_bundles = {d.name for d in local_dirs}
    all_bundles = sorted(set(local_bundles) | set(manifest_by_bundle.keys()), key=lambda x: x.lower())

    rows: list[dict[str, Any]] = []
    selectable_idx = 1
    for bundle in all_bundles:
        item = manifest_by_bundle.get(bundle)
        pdir = plugins_dir / bundle
        installed_version = _read_installed_version(pdir) if pdir.exists() else "-"
        if item is None:
            rows.append(
                {
                    "idx": selectable_idx,
                    "plugin_uid": "",
                    "bundle": bundle,
                    "display_name": bundle,
                    "install_bundle": bundle,
                    "status": "-",
                    "reason": "local only (not in server manifest)",
                    "installed_version": installed_version,
                    "server_version": "-",
                    "package": "-",
                    "item": None,
                    "selectable": False,
                    "exclusive_with": [],
                }
            )
            selectable_idx += 1
            continue

        install_bundle = _install_bundle_for_manifest_bundle(bundle, item)
        status, reason, installed_version = _plugin_status(
            plugins_dir,
            item,
            config.plugins_state_filename,
            install_bundle=install_bundle,
        )
        exclusive_with = sorted(_exclusive_counterparts(bundle, item))
        rows.append(
            {
                "idx": selectable_idx,
                "plugin_uid": _normalize_plugin_uid(str(item.get("plugin_uid", "")).strip()),
                "bundle": bundle,
                "display_name": str(item.get("display_name", "")).strip() or bundle,
                "install_bundle": install_bundle,
                "status": status,
                "reason": reason,
                "installed_version": installed_version,
                "server_version": str(item.get("version", "")).strip() or "-",
                "package": str(item.get("package", "")).strip(),
                "item": item,
                "selectable": True,
                "exclusive_with": exclusive_with,
            }
        )
        selectable_idx += 1

    return manifest_url, fallback_ip, install_root, rows


def _has_php_file_fast(root: Path, limit_files: int = 50000) -> bool:
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Skip hidden dirs to avoid accidental deep scans.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            seen += 1
            if fn.endswith(".php"):
                return True
            if seen >= limit_files:
                return True
    return False


def _plugin_status(
    plugins_dir: Path,
    plugin: dict[str, Any],
    state_filename: str,
    *,
    install_bundle: str | None = None,
) -> tuple[str, str, str]:
    bundle = str(plugin.get("bundle", "")).strip()
    install_name = str(install_bundle or "").strip() or bundle
    pdir = plugins_dir / install_name
    if not pdir.exists():
        return "MISSING", "not installed", "-"

    required = plugin.get("required_files")
    if isinstance(required, list):
        for rel in required:
            relp = pdir / str(rel)
            if not relp.exists():
                return "BROKEN", f"missing {rel}", _read_installed_version(pdir)
    else:
        if not _has_php_file_fast(pdir):
            return "BROKEN", "no php files", _read_installed_version(pdir)

    installed_version = _read_installed_version(pdir)
    expected_version = str(plugin.get("version", "")).strip() or "-"
    expected_sha = str(plugin.get("sha256", "")).strip()

    state: dict[str, Any] | None = None
    state_path = pdir / state_filename
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return "BROKEN", "invalid state metadata", installed_version

    # Canonical-path dev/stable aliases share one install directory.
    # Respect the last installed bundle from state file so UI/CLI won't
    # show both variants as simultaneously installed.
    if state is not None:
        recorded_bundle = str(state.get("bundle", "")).strip()
        if recorded_bundle and recorded_bundle != bundle:
            if recorded_bundle in _exclusive_counterparts(bundle, plugin):
                return "MISSING", f"counterpart installed={recorded_bundle}", "-"

    if installed_version == expected_version:
        if state is None:
            return "OK", "version match", installed_version
        installed_sha = str(state.get("sha256", "")).strip()
        if expected_sha and installed_sha == expected_sha:
            return "OK", "version+sha match", installed_version
        return "OK", "version match", installed_version

    return "UPDATE", f"installed={installed_version}", installed_version


def _extract_package(archive_path: Path, staging: Path) -> None:
    lower = archive_path.name.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(staging)
    else:
        with tarfile.open(archive_path, "r:*") as tf:
            tf.extractall(staging)
    _cleanup_macos_archive_artifacts(staging)


def _cleanup_macos_archive_artifacts(root: Path) -> None:
    """
    Remove macOS metadata artifacts (AppleDouble/._* and __MACOSX) that can
    introduce duplicate PHP classes and crash cache:clear.
    """
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        cur = Path(dirpath)
        for d in list(dirnames):
            if d == "__MACOSX":
                try:
                    shutil.rmtree(cur / d, ignore_errors=True)
                except Exception:
                    pass
                dirnames.remove(d)
        for fn in filenames:
            if fn.startswith("._"):
                try:
                    (cur / fn).unlink(missing_ok=True)
                except Exception:
                    pass


def _find_bundle_root(staging: Path, bundle: str) -> Path:
    direct = staging / bundle
    if direct.exists() and direct.is_dir():
        return direct
    dirs = [x for x in staging.iterdir() if x.is_dir()]
    if len(dirs) == 1:
        return dirs[0]
    for d in dirs:
        if d.name.lower().startswith(bundle.lower()) or d.name.lower().endswith("-main"):
            return d
    raise RuntimeError(f"cannot locate bundle root for {bundle} in {staging}")


def _set_owner_group(path: Path, owner_group: str = "www-data:www-data") -> None:
    subprocess.run(["chown", "-R", owner_group, str(path)], check=True)


def _install_or_replace_plugin(
    *,
    root: str,
    bundle: str,
    package_url: str,
    token: str | None,
    fallback_ip: str | None,
    state_filename: str,
    state_payload: dict[str, Any],
) -> None:
    plugins_dir = _resolve_plugins_dir(root, create=True)
    dst_dir = plugins_dir / bundle
    ts = int(time.time())
    backup_dir = plugins_dir / f".{bundle}.bak-{ts}"

    with tempfile.TemporaryDirectory(prefix=f"mcd-plugin-{bundle}-") as td:
        td_path = Path(td)
        archive_path = td_path / package_url.rsplit("/", 1)[-1]
        _fetch_file(package_url, token, archive_path, fallback_ip=fallback_ip)

        unpack_dir = td_path / "unpack"
        unpack_dir.mkdir(parents=True, exist_ok=True)
        _extract_package(archive_path, unpack_dir)
        source_dir = _find_bundle_root(unpack_dir, bundle)

        if dst_dir.exists():
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            dst_dir.rename(backup_dir)

        shutil.copytree(source_dir, dst_dir)
        (dst_dir / state_filename).write_text(json.dumps(state_payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        _set_owner_group(dst_dir)

        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)


def _run_plugin_template(config: AgentConfig, install, template: str) -> tuple[int, str]:
    return execute_mautic_command_template(
        php_bin=config.php_bin,
        run_as_user=config.mautic_run_as_user,
        root=install.root,
        template=template,
        timeout_sec=config.command_timeout_sec,
    )


def _run_plugin_cache_clear(config: AgentConfig, install) -> None:
    rc, out = _run_plugin_template(config, install, "cache:clear")
    if rc != 0:
        raise RuntimeError(f"cache:clear failed: {out}")


def _run_plugin_install_reload(config: AgentConfig, install) -> None:
    root = install.root

    def _is_metadata_null_reload_error(out: str) -> bool:
        text = str(out or "").lower()
        return (
            "pluginevent" in text
            and "metadata" in text
            and "must be of type array" in text
            and "null given" in text
        )

    def _repair_plugin_metadata_null_once() -> bool:
        if not install.db:
            return False
        db = MauticDB(install.db)
        try:
            if not db.table_has_column("{prefix}plugins", "metadata"):
                logging.info("[%s] plugin metadata repair skipped: plugins.metadata column not present", root)
                return False
        except Exception as e:
            logging.warning("[%s] plugin metadata column check failed: %s", root, e)
            return False
        repaired_any = False
        # Mautic bug workaround: malformed/null plugin metadata may break
        # mautic:plugin:install|reload with PluginUpdateEvent metadata=null.
        sql_fixes = [
            "UPDATE {prefix}plugins SET metadata = '[]' WHERE metadata IS NULL OR metadata = ''",
            "UPDATE {prefix}plugins SET metadata = '[]' WHERE metadata IS NOT NULL AND metadata <> '' AND JSON_VALID(metadata) = 0",
        ]
        for sql in sql_fixes:
            try:
                affected = db.execute_sql_template(sql)
                repaired_any = repaired_any or (int(affected) > 0)
                logging.info("[%s] plugin metadata repair affected=%s sql=%s", root, affected, sql)
            except Exception as e:
                # Keep going: some engines/schemas may reject JSON_VALID.
                logging.warning("[%s] plugin metadata repair skipped for sql=%s: %s", root, sql, e)
        return repaired_any

    # Keep this preflight unconditional: many Mautic 5/6/7 installs fail reload
    # before doing useful work if an older plugin row has NULL/broken metadata.
    _repair_plugin_metadata_null_once()

    rc, out = _run_plugin_template(config, install, "mautic:plugin:install")
    if rc == 0:
        return
    if _is_metadata_null_reload_error(out):
        repaired = _repair_plugin_metadata_null_once()
        if repaired:
            logging.info("[%s] retry mautic:plugin:install after metadata repair", root)
            rc2, out2 = _run_plugin_template(config, install, "mautic:plugin:install")
            if rc2 == 0:
                return
            raise RuntimeError(
                "mautic:plugin:install failed after metadata repair: "
                f"{out2}"
            )
    raise RuntimeError(f"mautic:plugin:install failed: {out}")


def _run_post_steps(config: AgentConfig, install) -> None:
    if config.plugins_post_cache_clear:
        _run_plugin_cache_clear(config, install)
    if config.plugins_post_install:
        _run_plugin_install_reload(config, install)


def _run_manifest_sql_fixes(config: AgentConfig, install, selected_rows: list[dict[str, Any]]) -> None:
    if not install.db:
        return
    db: MauticDB | None = None
    cluster_mode = bool(getattr(config, "cluster_id", None))
    for row in selected_rows:
        item = row.get("item")
        if not isinstance(item, dict):
            continue
        pre_sql = item.get("pre_sql")
        if not isinstance(pre_sql, list):
            continue
        bundle = str(row.get("bundle", "")).strip() or "plugin"
        for raw in pre_sql:
            sql = str(raw).strip()
            if not sql:
                continue
            if cluster_mode and _cluster_pre_sql_is_dangerous(sql):
                raise RuntimeError(f"pre_sql blocked in cluster mode for {bundle}: dangerous statement")
            try:
                if db is None:
                    db = MauticDB(install.db)
                affected = db.execute_sql_template(sql)
                logging.info("[%s] pre_sql %s affected=%s", install.root, bundle, affected)
            except Exception as e:
                raise RuntimeError(f"pre_sql failed for {bundle}: {e}") from e


def _cleanup_conflicting_plugin_rows(install, selected_rows: list[dict[str, Any]]) -> None:
    if not install.db:
        return
    selected_set = {
        str(row.get("bundle", "")).strip()
        for row in selected_rows
        if str(row.get("bundle", "")).strip()
    }
    conflicts: set[str] = set()
    for row in selected_rows:
        bundle = str(row.get("bundle", "")).strip()
        item = row.get("item")
        item_dict = item if isinstance(item, dict) else None
        for other in _exclusive_counterparts(bundle, item_dict):
            if other and other not in selected_set:
                conflicts.add(other)
    if not conflicts:
        return
    escaped = []
    for b in sorted(conflicts):
        escaped.append("'" + b.replace("'", "''") + "'")
    sql = f"DELETE FROM {{prefix}}plugins WHERE bundle IN ({', '.join(escaped)})"
    try:
        db = MauticDB(install.db)
        affected = db.execute_sql_template(sql)
        logging.info("[%s] plugin conflict rows cleanup affected=%s bundles=%s", install.root, affected, ",".join(sorted(conflicts)))
    except Exception as e:
        logging.warning("[%s] plugin conflict rows cleanup failed: %s", install.root, e)


def _apply_hostnet_mautic4_tx_patch(install, selected_rows: list[dict[str, Any]]) -> bool:
    if (install.mautic_major or 0) != 4:
        return False

    changed_any = False
    engine_path = Path(install.root) / "app" / "bundles" / "IntegrationsBundle" / "Migration" / "Engine.php"
    if not engine_path.exists():
        logging.warning("[%s] m4 tx patch skipped: Engine.php not found", install.root)
    else:
        text = engine_path.read_text(encoding="utf-8", errors="ignore")
        if "no active transaction" in text and "\\PDOException" in text:
            text = ""
        if text:
            commit_re = re.compile(
                r"^(?P<i>[ \t]*)\$conn = \$this->entityManager->getConnection\(\);\n"
                r"(?P=i)if \(\(method_exists\(\$conn, 'isTransactionActive'\) && \$conn->isTransactionActive\(\)\) \|\| "
                r"\(method_exists\(\$conn, 'getTransactionNestingLevel'\) && \$conn->getTransactionNestingLevel\(\) > 0\)\) \{\n"
                r"(?P=i)[ \t]{4}\$this->entityManager->commit\(\);\n"
                r"(?P=i)\}",
                flags=re.MULTILINE,
            )
            rollback_re = re.compile(
                r"^(?P<i>[ \t]*)\$conn = \$this->entityManager->getConnection\(\);\n"
                r"(?P=i)if \(\(method_exists\(\$conn, 'isTransactionActive'\) && \$conn->isTransactionActive\(\)\) \|\| "
                r"\(method_exists\(\$conn, 'getTransactionNestingLevel'\) && \$conn->getTransactionNestingLevel\(\) > 0\)\) \{\n"
                r"(?P=i)[ \t]{4}\$this->entityManager->rollback\(\);\n"
                r"(?P=i)\}",
                flags=re.MULTILINE,
            )
            bare_commit_re = re.compile(r"^([ \t]*)\$this->entityManager->commit\(\);\s*$", flags=re.MULTILINE)
            bare_rollback_re = re.compile(r"^([ \t]*)\$this->entityManager->rollback\(\);\s*$", flags=re.MULTILINE)

            def _commit_guard(indent: str) -> str:
                return (
                    f"{indent}$conn = $this->entityManager->getConnection();\n"
                    f"{indent}try {{\n"
                    f"{indent}    if ((method_exists($conn, 'isTransactionActive') && $conn->isTransactionActive()) || "
                    f"(method_exists($conn, 'getTransactionNestingLevel') && $conn->getTransactionNestingLevel() > 0)) {{\n"
                    f"{indent}        $this->entityManager->commit();\n"
                    f"{indent}    }}\n"
                    f"{indent}}} catch (\\PDOException $e) {{\n"
                    f"{indent}    if (false === stripos($e->getMessage(), 'no active transaction')) {{\n"
                    f"{indent}        throw $e;\n"
                    f"{indent}    }}\n"
                    f"{indent}}}"
                )

            def _rollback_guard(indent: str) -> str:
                return (
                    f"{indent}$conn = $this->entityManager->getConnection();\n"
                    f"{indent}try {{\n"
                    f"{indent}    if ((method_exists($conn, 'isTransactionActive') && $conn->isTransactionActive()) || "
                    f"(method_exists($conn, 'getTransactionNestingLevel') && $conn->getTransactionNestingLevel() > 0)) {{\n"
                    f"{indent}        $this->entityManager->rollback();\n"
                    f"{indent}    }}\n"
                    f"{indent}}} catch (\\PDOException $e) {{\n"
                    f"{indent}    if (false === stripos($e->getMessage(), 'no active transaction')) {{\n"
                    f"{indent}        throw $e;\n"
                    f"{indent}    }}\n"
                    f"{indent}}}"
                )

            def _replace_guarded_commit(m: re.Match[str]) -> str:
                return _commit_guard(m.group("i"))

            def _replace_guarded_rollback(m: re.Match[str]) -> str:
                return _rollback_guard(m.group("i"))

            new_text, n_commit = commit_re.subn(_replace_guarded_commit, text, count=1)
            new_text, n_rollback = rollback_re.subn(_replace_guarded_rollback, new_text, count=1)
            if n_commit == 0:
                new_text, n_commit = bare_commit_re.subn(lambda m: _commit_guard(m.group(1)), new_text, count=1)
            if n_rollback == 0:
                new_text, n_rollback = bare_rollback_re.subn(lambda m: _rollback_guard(m.group(1)), new_text, count=1)
            if new_text != text:
                engine_path.write_text(new_text, encoding="utf-8")
                changed_any = True
                logging.info("[%s] m4 tx patch (Engine.php) applied: commit=%s rollback=%s", install.root, n_commit, n_rollback)

    hostnet_path = Path(install.root) / "plugins" / "HostnetAuthBundle" / "HostnetAuthBundle.php"
    if not hostnet_path.exists():
        hostnet_path = _resolve_plugins_dir(install.root, create=False) / "HostnetAuthBundle" / "HostnetAuthBundle.php"
    if not hostnet_path.exists():
        return changed_any
    hostnet_text = hostnet_path.read_text(encoding="utf-8", errors="ignore")
    if "$db->beginTransaction();" in hostnet_text:
        block_re = re.compile(
            r"if \(!empty\(\$queries\)\) \{\s*\$db->beginTransaction\(\);\s*try \{\s*foreach \(\$queries as \$q\) \{\s*\$db->query\(\$q\);\s*\}\s*.*?\s*\}\s*catch \(\\Exception \$e\) \{\s*.*?\s*throw \$e;\s*\}\s*\}",
            flags=re.DOTALL,
        )
        repl = (
            "if (!empty($queries)) {\n"
            "            foreach ($queries as $q) {\n"
            "                $db->query($q);\n"
            "            }\n"
            "        }"
        )
        hostnet_new, n_blocks = block_re.subn(repl, hostnet_text)
        if n_blocks > 0:
            hostnet_path.write_text(hostnet_new, encoding="utf-8")
            changed_any = True
            logging.info("[%s] m4 tx patch (HostnetAuthBundle.php) applied: blocks=%s", install.root, n_blocks)
    return changed_any


def _apply_plugin_config_metadata_patch(install, selected_rows: list[dict[str, Any]]) -> bool:
    plugins_dir = _resolve_plugins_dir(install.root, create=False)
    changed_any = False
    for bundle in _selected_install_bundles(selected_rows):
        config_path = plugins_dir / bundle / "Config" / "config.php"
        if not config_path.exists():
            continue
        text = config_path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"['\"]metadata['\"]\s*=>", text):
            continue

        quote = '"' if re.search(r'^\s*"name"\s*=>', text, flags=re.MULTILINE) else "'"
        metadata_line = f"    {quote}metadata{quote}    => [],"
        new_text, count = re.subn(
            r"(?m)^(?P<indent>\s*)(?P<key>['\"]author['\"]\s*=>\s*[^,\n]+,\s*)$",
            lambda m: m.group(0) + "\n" + metadata_line,
            text,
            count=1,
        )
        if count == 0:
            new_text, count = re.subn(
                r"(?m)^(?P<indent>\s*)(?P<key>['\"]version['\"]\s*=>\s*[^,\n]+,\s*)$",
                lambda m: m.group(0) + "\n" + metadata_line,
                text,
                count=1,
            )
        if count == 0:
            logging.debug("[%s] plugin metadata config patch skipped for %s: insertion point not found", install.root, bundle)
            continue
        config_path.write_text(new_text, encoding="utf-8")
        changed_any = True
        logging.info("[%s] plugin metadata config patch applied: %s", install.root, bundle)
    return changed_any


def _selected_install_bundles(selected: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in selected:
        bundle = str(row.get("bundle", "") or "").strip()
        item = row.get("item")
        item_dict = item if isinstance(item, dict) else None
        install_bundle = str(row.get("install_bundle") or _install_bundle_for_manifest_bundle(bundle, item_dict)).strip()
        if install_bundle:
            out.append(install_bundle)
        if bundle and bundle != install_bundle:
            out.append(bundle)
    return _dedupe_text(out)


def _cluster_sync_bundle_names(
    selected: list[dict[str, Any]],
    *,
    action: str,
    auto_remove_bundles: list[str],
    rows_by_bundle: dict[str, dict[str, Any]],
) -> list[str]:
    names = list(_selected_install_bundles(selected))
    for conflict_bundle in auto_remove_bundles:
        conflict_row = rows_by_bundle.get(conflict_bundle)
        conflict_item = conflict_row.get("item") if isinstance(conflict_row, dict) else None
        conflict_install = str(
            (conflict_row or {}).get("install_bundle")
            or _install_bundle_for_manifest_bundle(conflict_bundle, conflict_item if isinstance(conflict_item, dict) else None)
        ).strip() or conflict_bundle
        names.extend([conflict_bundle, conflict_install])
    if action == "remove":
        names.extend(_selected_install_bundles(selected))
    return _dedupe_text(names)


def _apply_plugin_file_changes(
    *,
    config: AgentConfig,
    install,
    install_root: str,
    manifest_dir: str,
    fallback_ip: str | None,
    action: str,
    selected: list[dict[str, Any]],
    auto_remove_bundles: list[str],
    rows_by_bundle: dict[str, dict[str, Any]],
    run_post_steps: bool,
) -> bool:
    changed = False
    protected_install_paths = _protected_plugin_path_names(selected) if action != "remove" else set()
    if action != "remove" and auto_remove_bundles:
        for conflict_bundle in auto_remove_bundles:
            conflict_row = rows_by_bundle.get(conflict_bundle)
            conflict_item = conflict_row.get("item") if isinstance(conflict_row, dict) else None
            conflict_install = str(
                (conflict_row or {}).get("install_bundle")
                or _install_bundle_for_manifest_bundle(conflict_bundle, conflict_item if isinstance(conflict_item, dict) else None)
            ).strip() or conflict_bundle
            removed_paths: list[str] = []
            for name in sorted({conflict_bundle, conflict_install}):
                if name in protected_install_paths:
                    logging.info(
                        "[%s] skip auto-removing protected plugin path=%s selected=%s conflict=%s",
                        install_root,
                        name,
                        ",".join(sorted(protected_install_paths)),
                        conflict_bundle,
                    )
                    continue
                pdir = _resolve_plugins_dir(install_root, create=False) / name
                if _remove_plugin_path(pdir):
                    removed_paths.append(name)
            if removed_paths:
                changed = True
                logging.info(
                    "[%s] plugin %s auto-removed due to mutually exclusive selection (paths=%s)",
                    install_root,
                    conflict_bundle,
                    ",".join(removed_paths),
                )
                print(f"Auto-removed conflicting plugin: {conflict_bundle} ({', '.join(removed_paths)})")

    for row in selected:
        item = row["item"]
        bundle = row["bundle"]
        item_dict = item if isinstance(item, dict) else None
        install_bundle = str(row.get("install_bundle") or _install_bundle_for_manifest_bundle(bundle, item_dict))
        if action != "remove" and not isinstance(item, dict):
            logging.info("[%s] plugin %s not in server manifest, skip for action=%s", install_root, bundle, action)
            continue
        status = row["status"]
        if action == "remove":
            removed_paths: list[str] = []
            for name in _dedupe_text([install_bundle, bundle]):
                pdir = _resolve_plugins_dir(install_root, create=False) / name
                if _remove_plugin_path(pdir):
                    removed_paths.append(name)
            if removed_paths:
                changed = True
                logging.info("[%s] plugin %s removed (paths=%s)", install_root, bundle, ",".join(removed_paths))
            else:
                logging.info("[%s] plugin %s already absent (paths=%s)", install_root, bundle, ",".join(_dedupe_text([install_bundle, bundle])))
            continue

        assert isinstance(item, dict)
        package = row["package"]
        package_url = str(item.get("url", "")).strip()
        if not package_url:
            package_url = urllib.parse.urljoin(manifest_dir, package)

        should_apply = False
        if action == "install":
            should_apply = True
        elif action == "update":
            should_apply = status in {"UPDATE", "MISSING"}
        elif action == "reinstall":
            should_apply = True
        else:
            should_apply = status in {"MISSING", "UPDATE", "BROKEN"}

        if not should_apply:
            logging.info("[%s] plugin %s skip action=%s status=%s", install_root, bundle, action, status)
            continue

        mailer_preflight_names = {
            bundle,
            install_bundle,
            str(item.get("display_name", "")).strip(),
            str(item.get("package", "")).strip(),
            package_url,
        }
        ensure_mailer_packages_for_bundles(
            config=config,
            root=install_root,
            console_path=install.console_path,
            bundles={x for x in mailer_preflight_names if x},
            reason="plugins-apply",
        )

        _install_or_replace_plugin(
            root=install_root,
            bundle=install_bundle,
            package_url=package_url,
            token=config.mcc_token,
            fallback_ip=fallback_ip,
            state_filename=config.plugins_state_filename,
            state_payload={
                "bundle": bundle,
                "version": str(item.get("version", "")).strip(),
                "sha256": str(item.get("sha256", "")).strip(),
                "installed_at": datetime_now_iso(),
                "source": package_url,
            },
        )
        if install_bundle != bundle:
            alias_path = _resolve_plugins_dir(install_root, create=False) / bundle
            if alias_path.exists() or alias_path.is_symlink():
                try:
                    if alias_path.is_symlink() or alias_path.is_file():
                        alias_path.unlink()
                    elif alias_path.is_dir():
                        shutil.rmtree(alias_path)
                    logging.info("[%s] removed alias plugin path=%s (installed as %s)", install_root, bundle, install_bundle)
                except Exception as e:
                    logging.warning("[%s] failed to cleanup alias plugin path=%s: %s", install_root, bundle, e)
        changed = True
        logging.info("[%s] plugin %s applied action=%s path=%s", install_root, bundle, action, install_bundle)

    compatibility_changed = False
    if action != "remove" and selected:
        # Compatibility patches are recovery steps too. A previous run can copy
        # plugin files successfully and then fail during Mautic reload, leaving
        # the plugin version at OK while core reload still needs patching.
        compatibility_changed = _apply_hostnet_mautic4_tx_patch(install, selected) or compatibility_changed
        compatibility_changed = _apply_plugin_config_metadata_patch(install, selected) or compatibility_changed

    if changed or compatibility_changed:
        _run_manifest_sql_fixes(config, install, selected)
        _cleanup_conflicting_plugin_rows(install, selected)
        if run_post_steps:
            _run_post_steps(config, install)
    return changed or compatibility_changed


def _cluster_plugin_cli_args(
    config: AgentConfig,
    *,
    root: str,
    action: str,
    selected: list[dict[str, Any]],
    force_bundles: list[str] | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    exe = shutil.which("mcd-cli") or "mcd-cli"
    args = [
        exe,
        "plugins",
        "--config",
        str(getattr(config, "config_file_path", "") or "/opt/mcd/etc/mcd.toml"),
        "--root",
        str(root),
        "--action",
        str(action),
        "--yes",
        "--no-color",
    ]
    if force_bundles is not None:
        for bundle in force_bundles:
            args.extend(["--bundle", str(bundle)])
        if extra_args:
            args.extend(extra_args)
        return args
    plugin_uids = [
        str(row.get("plugin_uid", "") or "").strip()
        for row in selected
        if str(row.get("plugin_uid", "") or "").strip() and action != "remove"
    ]
    if plugin_uids and len(plugin_uids) == len(selected):
        for uid in plugin_uids:
            args.extend(["--plugin-uid", uid])
    else:
        for bundle in _selected_install_bundles(selected):
            if _is_valid_bundle_name(bundle):
                args.extend(["--bundle", bundle])
    if extra_args:
        args.extend(extra_args)
    return args


def _cluster_plugin_enqueue_manual_request(
    config: AgentConfig,
    *,
    root: str,
    task_type: str,
    entity_id: int | None,
    command_str: str,
    timeout_sec: int,
    target_host_name: str,
) -> int:
    names = mysql_state_table_names(config)
    table = names["manual_requests"]
    now_s = time.time()
    conn = mysql_state_existing_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO `{table}`(
                  host_name, root, task_type, entity_id, command_str, timeout_sec, status, requested_at
                ) VALUES(%s,%s,%s,%s,%s,%s,'pending',%s)
                """,
                (
                    str(target_host_name),
                    str(root),
                    str(task_type),
                    entity_id,
                    str(command_str),
                    int(timeout_sec),
                    now_s,
                ),
            )
            req_id = int(cur.lastrowid or 0)
        conn.commit()
        if req_id <= 0:
            raise RuntimeError("manual request insert returned empty id")
        return req_id
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _cluster_plugin_manual_request_status(config: AgentConfig, req_id: int, host_name: str) -> str | None:
    names = mysql_state_table_names(config)
    table = names["manual_requests"]
    conn = mysql_state_existing_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT status FROM `{table}` WHERE id=%s AND host_name=%s LIMIT 1",
                (int(req_id), str(host_name)),
            )
            row = cur.fetchone()
        if not isinstance(row, dict):
            return None
        return str(row.get("status") or "")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _wait_manual_requests(
    config: AgentConfig,
    reqs: list[tuple[str, int]],
    *,
    timeout_sec: int,
    ok_statuses: set[str] | None = None,
) -> tuple[bool, dict[tuple[str, int], str]]:
    ok = ok_statuses or {"done"}
    terminal = {"done", "failed", "timeout", "lost", "skipped", "cancelled"}
    deadline = time.time() + max(1, int(timeout_sec))
    statuses: dict[tuple[str, int], str] = {}
    while time.time() < deadline:
        all_terminal = True
        for host, req_id in reqs:
            key = (host, req_id)
            st = _cluster_plugin_manual_request_status(config, req_id, host) or "unknown"
            st_l = st.strip().lower()
            statuses[key] = st_l
            if st_l not in terminal:
                all_terminal = False
        if all_terminal:
            return all(st in ok for st in statuses.values()), statuses
        time.sleep(1.0)
    return False, statuses


def _cluster_plugin_note_node(
    config: AgentConfig,
    *,
    key: str,
    host: str,
    status: str,
    message: str,
    digest: str = "",
) -> None:
    now_s = int(time.time())

    def mutator(payload: dict[str, Any]) -> None:
        payload.clear()
        payload.update(
            {
                "kind": "plugin_node_status",
                "cluster_id": str(getattr(config, "cluster_id", "") or ""),
                "operation_key": key,
                "host": str(host),
                "status": status,
                "message": message[:500],
                "digest": digest,
                "updated_at": now_s,
            }
        )

    _cluster_plugin_mutate(config, key, mutator, host_name=host)


def _cluster_plugin_sync_check(
    *,
    config: AgentConfig,
    root: str,
    key: str,
    expected_digest: str,
    bundles: list[str],
) -> int:
    local_host = _cluster_local_host_name(config)
    timeout_sec = max(30, int(getattr(config, "plugins_cluster_file_sync_wait_sec", 900) or 900))
    deadline = time.time() + timeout_sec
    last_digest = ""
    while time.time() < deadline:
        payload = _plugin_selection_digest(root, bundles)
        last_digest = str(payload.get("digest") or "")
        if last_digest == expected_digest and str(payload.get("status") or "ok") == "ok":
            _cluster_plugin_note_node(
                config,
                key=key,
                host=local_host,
                status="files_synced",
                message="plugin files match reference digest",
                digest=last_digest,
            )
            print(f"cluster plugin sync check ok host={local_host} digest={last_digest}")
            return 0
        time.sleep(5.0)
    _cluster_plugin_note_node(
        config,
        key=key,
        host=local_host,
        status="files_sync_failed",
        message=f"plugin files did not match reference digest before timeout expected={expected_digest} got={last_digest}",
        digest=last_digest,
    )
    print(f"cluster plugin sync check failed host={local_host} expected={expected_digest} got={last_digest}")
    return 1


def _cluster_plugin_wait_local_file_sync(
    *,
    config: AgentConfig,
    root: str,
    key: str,
    host: str,
    expected_digest: str,
    bundles: list[str],
    timeout_sec: int,
) -> None:
    deadline = time.time() + max(1, int(timeout_sec))
    last_digest = ""
    while time.time() < deadline:
        payload = _plugin_selection_digest(root, bundles)
        last_digest = str(payload.get("digest") or "")
        if last_digest == expected_digest and str(payload.get("status") or "ok") == "ok":
            _cluster_plugin_note_node(
                config,
                key=key,
                host=host,
                status="files_synced",
                message="plugin files match reference digest",
                digest=last_digest,
            )
            return
        time.sleep(5.0)
    _cluster_plugin_note_node(
        config,
        key=key,
        host=host,
        status="files_sync_failed",
        message=f"plugin files did not match reference digest before timeout expected={expected_digest} got={last_digest}",
        digest=last_digest,
    )
    raise RuntimeError(f"cluster plugin local file sync check failed: {host}:expected={expected_digest}:got={last_digest}")


def _cluster_plugin_wait_file_sync(
    *,
    config: AgentConfig,
    install,
    key: str,
    request_hash: str,
    expected_hosts: list[str],
    reference_host: str,
    sync_bundles: list[str],
    expected_digest: str,
) -> None:
    _cluster_plugin_set_phase(
        config,
        key,
        phase="file_sync",
        local_host=reference_host,
        request_hash=request_hash,
        message="waiting for plugin files to sync on cluster nodes",
        extra={"expected_digest": expected_digest, "sync_bundles": sync_bundles},
    )
    reqs: list[tuple[str, int]] = []
    args = _cluster_plugin_cli_args(
        config,
        root=install.root,
        action="auto",
        selected=[],
        force_bundles=sync_bundles,
        extra_args=[
            "--cluster-sync-check-key",
            key,
            "--cluster-sync-check-digest",
            expected_digest,
        ],
    )
    command_payload = _CMD_SEP.join(args)
    timeout_sec = max(30, int(getattr(config, "plugins_cluster_file_sync_wait_sec", 900) or 900))
    for host in expected_hosts:
        if _cluster_plugin_host_is_local(config, host):
            _cluster_plugin_wait_local_file_sync(
                config=config,
                root=install.root,
                key=key,
                host=host,
                expected_digest=expected_digest,
                bundles=sync_bundles,
                timeout_sec=timeout_sec,
            )
            continue
        req_id = _cluster_plugin_enqueue_manual_request(
            config,
            root=install.root,
            task_type="plugins_sync_check",
            entity_id=None,
            command_str=command_payload,
            timeout_sec=timeout_sec + 30,
            target_host_name=host,
        )
        reqs.append((host, req_id))
    statuses: dict[tuple[str, int], str] = {}
    if reqs:
        ok, statuses = _wait_manual_requests(config, reqs, timeout_sec=timeout_sec + 60)
        if not ok:
            parts = [f"{host}:request_id={req_id}:status={statuses.get((host, req_id), 'unknown')}" for host, req_id in reqs]
            raise RuntimeError("cluster plugin file sync check failed: " + " ".join(parts))
    bad_nodes: list[str] = []
    for host in expected_hosts:
        payload = _cluster_plugin_read_payload(config, key, host_name=host)
        status = str(payload.get("status") or "").strip()
        digest = str(payload.get("digest") or "").strip()
        if status != "files_synced" or digest != expected_digest:
            bad_nodes.append(f"{host}:status={status or '-'}:digest={digest or '-'}")
    if bad_nodes:
        raise RuntimeError("cluster plugin file digest status mismatch: " + " ".join(bad_nodes))


def _cluster_plugin_cache_clear_all(
    *,
    config: AgentConfig,
    install,
    key: str,
    request_hash: str,
    expected_hosts: list[str],
    reference_host: str,
) -> None:
    _cluster_plugin_set_phase(
        config,
        key,
        phase="cache_clear",
        local_host=reference_host,
        request_hash=request_hash,
        message="running cache:clear on all cluster nodes",
    )
    args = build_mautic_exec_args(
        php_bin=config.php_bin,
        root=install.root,
        command="cache:clear",
        instance_id=None,
        run_as_user=config.mautic_run_as_user,
    )
    command_payload = _CMD_SEP.join(args)
    timeout_sec = max(60, int(getattr(config, "plugins_cluster_cache_clear_wait_sec", 600) or 600))
    reqs: list[tuple[str, int]] = []
    local_cache_cleared = False
    for host in expected_hosts:
        if _cluster_plugin_host_is_local(config, host):
            if not local_cache_cleared:
                _run_plugin_cache_clear(config, install)
                local_cache_cleared = True
            continue
        req_id = _cluster_plugin_enqueue_manual_request(
            config,
            root=install.root,
            task_type="cache_clear",
            entity_id=None,
            command_str=command_payload,
            timeout_sec=timeout_sec,
            target_host_name=host,
        )
        reqs.append((host, req_id))
    if reqs:
        ok, statuses = _wait_manual_requests(config, reqs, timeout_sec=timeout_sec + 60, ok_statuses={"done", "skipped"})
        if not ok:
            parts = [f"{host}:request_id={req_id}:status={statuses.get((host, req_id), 'unknown')}" for host, req_id in reqs]
            raise RuntimeError("cluster plugin cache clear failed: " + " ".join(parts))


def _cluster_plugin_wait_payload_done(config: AgentConfig, key: str, request_hash: str, *, timeout_sec: int) -> tuple[bool, str]:
    deadline = time.time() + max(1, int(timeout_sec))
    last_msg = ""
    while time.time() < deadline:
        payload = _cluster_plugin_read_payload(config, key)
        phase = str(payload.get("phase", "") or "").strip()
        if str(payload.get("request_hash", "") or "") == request_hash:
            last_msg = str(payload.get("message", "") or "")
            if phase == "done":
                return True, last_msg or "cluster plugin operation completed"
            if phase == "failed":
                return False, last_msg or "cluster plugin operation failed"
        time.sleep(2.0)
    return False, last_msg or "cluster plugin operation wait timed out"


def _cluster_plugin_delegate_to_reference(
    *,
    config: AgentConfig,
    install,
    action: str,
    selected: list[dict[str, Any]],
    key: str,
    request_hash: str,
    reference_host: str,
) -> int:
    args = _cluster_plugin_cli_args(config, root=install.root, action=action, selected=selected)
    req_id = _cluster_plugin_enqueue_manual_request(
        config,
        root=install.root,
        task_type="plugins_apply",
        entity_id=None,
        command_str=_CMD_SEP.join(args),
        timeout_sec=max(60, int(getattr(config, "command_timeout_sec", 1800) or 1800)),
        target_host_name=reference_host,
    )
    print(f"Cluster plugin operation delegated to reference node {reference_host}: request_id={req_id}")
    timeout_sec = max(60, int(getattr(config, "command_timeout_sec", 1800) or 1800))
    ok, statuses = _wait_manual_requests(config, [(reference_host, req_id)], timeout_sec=timeout_sec, ok_statuses={"done", "skipped"})
    if ok:
        done, msg = _cluster_plugin_wait_payload_done(config, key, request_hash, timeout_sec=60)
        if done:
            print(msg)
            return 0
        status = statuses.get((reference_host, req_id), "unknown")
        print(f"Cluster plugin reference request finished status={status}: {msg}")
        return 0 if status == "done" else 1
    status = statuses.get((reference_host, req_id), "unknown")
    print(f"Cluster plugin reference request failed status={status}")
    return 1


def _run_cluster_plugin_operation(
    *,
    config: AgentConfig,
    install,
    install_root: str,
    manifest_dir: str,
    fallback_ip: str | None,
    action: str,
    selected: list[dict[str, Any]],
    auto_remove_bundles: list[str],
    rows_by_bundle: dict[str, dict[str, Any]],
) -> int:
    if not mysql_state_enabled(config):
        raise RuntimeError("cluster plugin operation requires mysql_hybrid state backend")
    reference_host = _cluster_plugin_reference_host(config)
    local_host = _cluster_local_host_name(config)
    expected_hosts = _cluster_plugin_expected_hosts(config)
    key = _cluster_plugin_sync_key(config, install.root)
    request_hash = _cluster_plugin_row_signature(action, selected, auto_remove_bundles)
    if not _cluster_plugin_local_is_reference(config):
        return _cluster_plugin_delegate_to_reference(
            config=config,
            install=install,
            action=action,
            selected=selected,
            key=key,
            request_hash=request_hash,
            reference_host=reference_host,
        )

    invocation_id = uuid.uuid4().hex
    begin = _cluster_plugin_begin_reference(
        config,
        key,
        invocation_id=invocation_id,
        request_hash=request_hash,
        root=install.root,
        action=action,
        expected_hosts=expected_hosts,
        reference_host=reference_host,
        local_host=local_host,
    )
    begin_action = str((begin or {}).get("action") or "")
    if begin_action == "wait":
        ok, msg = _cluster_plugin_wait_payload_done(
            config,
            key,
            request_hash,
            timeout_sec=max(60, int(getattr(config, "command_timeout_sec", 1800) or 1800)),
        )
        print(msg)
        return 0 if ok else 1
    if begin_action == "busy":
        raise RuntimeError(str((begin or {}).get("message") or "another cluster plugin operation is active"))

    print(f"Cluster plugin operation reference={reference_host} local={local_host} nodes={','.join(expected_hosts)}")
    try:
        _cluster_plugin_set_phase(
            config,
            key,
            phase="files_running",
            local_host=local_host,
            request_hash=request_hash,
            message="applying plugin files on reference node",
            node_status="files_running",
        )
        changed = _apply_plugin_file_changes(
            config=config,
            install=install,
            install_root=install_root,
            manifest_dir=manifest_dir,
            fallback_ip=fallback_ip,
            action=action,
            selected=selected,
            auto_remove_bundles=auto_remove_bundles,
            rows_by_bundle=rows_by_bundle,
            run_post_steps=False,
        )
        if not changed and action != "remove":
            _cluster_plugin_set_phase(
                config,
                key,
                phase="done",
                local_host=local_host,
                request_hash=request_hash,
                message="No plugin changes required",
                node_status="done",
            )
            print("No plugin changes required")
            return 0

        sync_bundles = _cluster_sync_bundle_names(
            selected,
            action=action,
            auto_remove_bundles=auto_remove_bundles,
            rows_by_bundle=rows_by_bundle,
        )
        digest_payload = _plugin_selection_digest(install.root, sync_bundles)
        expected_digest = str(digest_payload.get("digest") or "")
        _cluster_plugin_set_phase(
            config,
            key,
            phase="files_done",
            local_host=local_host,
            request_hash=request_hash,
            message="reference plugin files updated",
            node_status="files_done",
            extra={"expected_digest": expected_digest, "sync_bundles": sync_bundles},
        )
        _cluster_plugin_wait_file_sync(
            config=config,
            install=install,
            key=key,
            request_hash=request_hash,
            expected_hosts=expected_hosts,
            reference_host=reference_host,
            sync_bundles=sync_bundles,
            expected_digest=expected_digest,
        )
        if config.plugins_post_cache_clear:
            _cluster_plugin_cache_clear_all(
                config=config,
                install=install,
                key=key,
                request_hash=request_hash,
                expected_hosts=expected_hosts,
                reference_host=reference_host,
            )
        if config.plugins_post_install:
            _cluster_plugin_set_phase(
                config,
                key,
                phase="plugin_install",
                local_host=local_host,
                request_hash=request_hash,
                message="running mautic:plugin:install on reference node",
                node_status="plugin_install",
            )
            _run_plugin_install_reload(config, install)
        _cluster_plugin_set_phase(
            config,
            key,
            phase="done",
            local_host=local_host,
            request_hash=request_hash,
            message="Cluster plugin operation completed",
            node_status="done",
        )
        print("Cluster plugin operation completed")
        return 0
    except Exception as e:
        try:
            _cluster_plugin_set_phase(
                config,
                key,
                phase="failed",
                local_host=local_host,
                request_hash=request_hash,
                message=str(e),
                node_status="failed",
            )
        except Exception:
            pass
        raise


def run_plugins_interactive(
    *,
    config: AgentConfig,
    root: str | None,
    selection: str | None,
    bundles: list[str] | None = None,
    plugin_uids: list[str] | None = None,
    action: str | None,
    no_color: bool,
    yes: bool,
    list_available: bool = False,
    list_installed: bool = False,
    catalog_json: bool = False,
    cluster_sync_check_key: str | None = None,
    cluster_sync_check_digest: str | None = None,
) -> int:
    if (list_available or list_installed) and root is None:
        installs = discover_mautic(
            config.discovery_roots,
            config.exclude_path_contains,
            config.supported_mautic_majors,
            config.custom_instances,
        )
        if not installs:
            raise RuntimeError("No Mautic install found")
        rc = 0
        for inst in installs:
            print("")
            print(f"=== Instance: {inst.root} ===")
            try:
                run_plugins_interactive(
                    config=config,
                    root=inst.root,
                    selection=selection,
                    bundles=bundles,
                    plugin_uids=plugin_uids,
                    action=action,
                    no_color=no_color,
                    yes=yes,
                    list_available=list_available,
                    list_installed=list_installed,
                    catalog_json=catalog_json,
                    cluster_sync_check_key=cluster_sync_check_key,
                    cluster_sync_check_digest=cluster_sync_check_digest,
                )
            except Exception as e:
                rc = 1
                print(f"Plugins list error for {inst.root}: {e}")
        return rc

    install = _select_install_with_db(config, root)
    if cluster_sync_check_key or cluster_sync_check_digest:
        key = str(cluster_sync_check_key or "").strip()
        digest = str(cluster_sync_check_digest or "").strip()
        if not key or not digest:
            raise RuntimeError("cluster sync check requires key and expected digest")
        check_bundles = [str(x).strip() for x in (bundles or []) if str(x).strip()]
        if not check_bundles:
            raise RuntimeError("cluster sync check requires at least one --bundle")
        return _cluster_plugin_sync_check(
            config=config,
            root=install.root,
            key=key,
            expected_digest=digest,
            bundles=check_bundles,
        )
    major = install.mautic_major or 6
    if not catalog_json:
        print("Loading plugin manifest...")
    try:
        manifest_url, fallback_ip, install_root, rows = _build_plugin_rows(config=config, install=install)
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot fetch manifest (network/timeout): {e}") from e

    if not rows:
        print("No plugin rows")
        return 0

    manifest_dir = manifest_url.rsplit("/", 1)[0] + "/"
    plugins_dir = _resolve_plugins_dir(install_root, create=True)

    idx_map: dict[int, dict[str, Any]] = {}
    for row in rows:
        idx = row["idx"]
        if isinstance(idx, int):
            idx_map[idx] = row

    if list_available:
        print(f"Mautic root: {install_root}")
        print(f"Mautic major: {major}")
        print(f"Manifest: {manifest_url}")
        print("")
        print("Available plugins from server manifest:")
        print("Bundle                      Server       Package")
        print("--------------------------  -----------  ------------------------------")
        for row in rows:
            if row["item"] is None:
                continue
            print(f"{str(row['bundle']):<26}  {str(row['server_version']):<11}  {str(row['package'])}")
        return 0

    if list_installed:
        print(f"Mautic root: {install_root}")
        print(f"Mautic major: {major}")
        print(f"Manifest: {manifest_url}")
        print("")
        print("Installed plugins on host:")
        print("Bundle                      Installed")
        print("--------------------------  -----------")
        shown = 0
        for row in rows:
            if str(row["installed_version"]) == "-":
                continue
            print(f"{str(row['bundle']):<26}  {str(row['installed_version']):<11}")
            shown += 1
        if shown == 0:
            print("(none)")
        return 0

    if catalog_json:
        payload_rows: list[dict[str, Any]] = []
        for row in rows:
            payload_rows.append(
                {
                    "idx": row.get("idx"),
                    "plugin_uid": str(row.get("plugin_uid", "")).strip(),
                    "bundle": str(row.get("bundle", "")).strip(),
                    "display_name": str(row.get("display_name", "")).strip(),
                    "install_bundle": str(row.get("install_bundle", "")).strip(),
                    "status": str(row.get("status", "")).strip(),
                    "reason": str(row.get("reason", "")).strip(),
                    "installed_version": str(row.get("installed_version", "")).strip(),
                    "server_version": str(row.get("server_version", "")).strip(),
                    "selectable": bool(row.get("selectable", False)),
                    "exclusive_with": list(row.get("exclusive_with", []) or []),
                }
            )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "root": install_root,
                    "mautic_major": major,
                    "manifest_url": manifest_url,
                    "fallback_ip": fallback_ip or "",
                    "items": payload_rows,
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 0

    print(f"Mautic root: {install_root}")
    print(f"Mautic major: {major}")
    print(f"Manifest: {manifest_url}")
    print("")
    print("Idx  Status   Bundle                      Installed    Server")
    print("---  -------  --------------------------  -----------  -----------")
    for row in rows:
        idx = row["idx"]
        idx_cell = f"{idx:>3}" if isinstance(idx, int) else "  -"
        print(
            f"{idx_cell}  {_status_cell(str(row['status']), no_color)}  "
            f"{str(row['bundle']):<26}  {str(row['installed_version']):<11}  {str(row['server_version']):<11}"
        )
    print("")

    selected: list[dict[str, Any]] = []
    auto_remove_bundles: list[str] = []
    while True:
        if action is None:
            print("Action:")
            print("a = auto, i = install, u = update, r = reinstall, d = remove, x = exit")
            action_in = _ask("Choose action [a/i/u/r/d/x], default=a: ")
        else:
            action_in = action
        normalized = _normalize_action(action_in)
        action = None
        if normalized is None:
            print("Invalid action, try again")
            continue
        if normalized == "exit":
            print("No changes")
            return 0
        chosen_action = normalized

        if selection is None and not bundles and not plugin_uids:
            selection_in = _ask("Select plugins to apply (e.g. 1-3 6 10), empty=back: ").strip()
        elif bundles or plugin_uids:
            selection_in = ""
        else:
            selection_in = selection.strip()
            selection = None

        if not selection_in and not bundles and not plugin_uids:
            if not sys.stdin.isatty():
                print("No selection provided in non-interactive mode, exit")
                return 0
            print("Back to action selection")
            continue

        if plugin_uids:
            requested_uids = {
                _normalize_plugin_uid(str(x or ""))
                for x in (plugin_uids or [])
                if _normalize_plugin_uid(str(x or ""))
            }
            requested_uids = {x for x in requested_uids if _valid_plugin_uid(x)}
            if not requested_uids:
                print("No valid plugin uid values, try again")
                continue
            selected = [
                row for row in rows
                if bool(row.get("selectable", False)) and str(row.get("plugin_uid", "")).strip() in requested_uids
            ]
            found = {str(row.get("plugin_uid", "")).strip() for row in selected}
            missing = sorted(requested_uids - found)
            if missing:
                raise RuntimeError(f"plugin uid(s) not found in manifest for this instance: {', '.join(missing)}")
        elif bundles:
            requested = {str(x or "").strip() for x in (bundles or []) if str(x or "").strip()}
            if not requested:
                print("No valid plugin bundle names, try again")
                continue
            selected = [
                row for row in rows
                if bool(row.get("selectable", False)) and str(row.get("bundle", "")).strip() in requested
            ]
            found = {str(row.get("bundle", "")).strip() for row in selected}
            missing = sorted(requested - found)
            if missing:
                raise RuntimeError(f"plugin bundle(s) not found in manifest for this instance: {', '.join(missing)}")
        else:
            indexes = _parse_selection(selection_in, max(idx_map.keys()) if idx_map else 0)
            if not indexes:
                print("No valid plugin indexes, try again")
                continue
            selected = [idx_map[i] for i in indexes if i in idx_map]
        if not selected:
            print("Selected indexes are not actionable, try again")
            continue

        try:
            _validate_selected_exclusive_conflicts(selected)
        except RuntimeError as e:
            print(f"Selection error: {e}")
            if not sys.stdin.isatty():
                return 1
            print("Back to action selection")
            continue

        auto_remove_bundles = []
        if chosen_action != "remove":
            auto_remove_bundles = _auto_remove_conflicting_installed_bundles(
                selected,
                plugins_dir,
            )

        print("Selected:")
        for row in selected:
            print(f"- {row['bundle']} [{row['status']}]")
        if auto_remove_bundles:
            print("Will auto-remove conflicting installed bundle(s):")
            for b in auto_remove_bundles:
                print(f"- {b}")
        if not yes:
            confirm = _ask("Apply selected plugins? [y/N]: ").strip().lower()
            if confirm not in {"y", "yes"}:
                print("Cancelled, back to action selection")
                continue
        break

    action = chosen_action

    rows_by_bundle = {
        str(row.get("bundle", "")).strip(): row
        for row in rows
        if str(row.get("bundle", "")).strip()
    }
    if _cluster_plugin_required(config):
        return _run_cluster_plugin_operation(
            config=config,
            install=install,
            install_root=install_root,
            manifest_dir=manifest_dir,
            fallback_ip=fallback_ip,
            action=action,
            selected=selected,
            auto_remove_bundles=auto_remove_bundles,
            rows_by_bundle=rows_by_bundle,
        )

    changed = _apply_plugin_file_changes(
        config=config,
        install=install,
        install_root=install_root,
        manifest_dir=manifest_dir,
        fallback_ip=fallback_ip,
        action=action,
        selected=selected,
        auto_remove_bundles=auto_remove_bundles,
        rows_by_bundle=rows_by_bundle,
        run_post_steps=True,
    )
    if changed:
        print("Plugins applied and post-steps completed")
    else:
        print("No plugin changes required")
    return 0


def datetime_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
