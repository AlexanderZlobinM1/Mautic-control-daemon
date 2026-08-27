from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import urlparse

from mcd_agent.config import ManualInstanceConfig
from mcd_agent.instance_uid import build_domain_uid
from mcd_agent.localphp import parse_local_php
from mcd_agent.models import DBConfig, MauticInstall


_TEMPLATE_DOMAIN_RE = re.compile(r"^default[0-9a-z-]*\.sales-snap\.(com|ru)$", flags=re.IGNORECASE)
RETIRED_INSTANCE_MARKER = ".mcd-retired-after-composer-move"


def _is_template_domain(host: str | None) -> bool:
    v = str(host or "").strip().lower()
    if not v:
        return False
    return bool(_TEMPLATE_DOMAIN_RE.match(v))


def _domain_preference_score(host: str | None) -> int:
    v = str(host or "").strip().lower()
    if not v:
        return -1000
    score = 0
    if v not in {"_", "localhost"}:
        score += 20
    if "*" not in v:
        score += 10
    if _is_template_domain(v):
        score -= 60
    else:
        score += 40
    score += v.count(".")
    return score


def _find_console_path(root: str, explicit: str | None = None) -> str | None:
    if explicit:
        candidate = Path(explicit)
        if candidate.exists():
            return str(candidate)

    root_path = Path(root)
    console_bin = root_path / "bin" / "console"
    console_legacy = root_path / "app" / "console"

    if console_bin.exists():
        return str(console_bin)
    if console_legacy.exists():
        return str(console_legacy)
    return None


def _resolve_root_from_local_php(path: Path) -> str | None:
    s = str(path)
    if s.endswith("/app/config/local.php"):
        return str(path.parent.parent.parent)
    if s.endswith("/config/local.php"):
        return str(path.parent.parent)
    return None


def _detect_major_from_composer_lock(root: str) -> int | None:
    lock_path = Path(root) / "composer.lock"
    if not lock_path.exists():
        return None
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return None

    packages = data.get("packages", [])
    if not isinstance(packages, list):
        return None

    known_names = {"mautic/core-lib", "mautic/core-bundle", "mautic/core"}
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        name = str(pkg.get("name", "")).strip()
        if name not in known_names:
            continue
        version = str(pkg.get("version", "")).strip()
        digits = "".join(ch for ch in version if ch.isdigit() or ch == ".")
        major = digits.split(".", 1)[0] if digits else ""
        try:
            return int(major)
        except ValueError:
            continue
    return None


def _detect_mautic_major(root: str, local_php_path: str | None, manual_major: int | None = None) -> int | None:
    if manual_major is not None:
        return manual_major
    if local_php_path:
        if local_php_path.endswith("/app/config/local.php"):
            return 4
        if local_php_path.endswith("/config/local.php"):
            # v5+ path; try exact major from composer.lock, fallback to 5.
            return _detect_major_from_composer_lock(root) or 5
    return _detect_major_from_composer_lock(root)


def _build_db(local_php_path: str | None, override: ManualInstanceConfig | None = None) -> DBConfig | None:
    base: dict[str, str] = {}
    if local_php_path and Path(local_php_path).exists():
        base = parse_local_php(local_php_path)

    db_host = (override.db_host if override else None) or base.get("db_host")
    db_prefix = (override.db_table_prefix if override else None) or base.get("db_table_prefix")
    db_name = (override.db_name if override else None) or base.get("db_name")
    db_user = (override.db_user if override else None) or base.get("db_user")
    db_password = (override.db_password if override else None) or base.get("db_password")

    db_port_raw: str | int | None = (override.db_port if override else None) or base.get("db_port")
    try:
        db_port = int(db_port_raw) if db_port_raw is not None else 3306
    except ValueError:
        db_port = 3306

    if not (db_host and db_name and db_user and db_password):
        return None

    return DBConfig(
        host=db_host,
        port=db_port,
        name=db_name,
        user=db_user,
        password=db_password,
        table_prefix=db_prefix or "",
    )


def _read_mautic_timezone(local_php_path: str | None) -> str | None:
    if not local_php_path or not Path(local_php_path).exists():
        return None
    data = parse_local_php(local_php_path)
    tz = (data.get("default_timezone") or data.get("timezone") or "").strip()
    return tz or None


def _domain_from_local_php(local_php_path: str | None) -> str | None:
    if not local_php_path or not Path(local_php_path).exists():
        return None
    data = parse_local_php(local_php_path)
    site_url = (data.get("site_url") or "").strip()
    if not site_url:
        return None
    try:
        parsed = urlparse(site_url if "://" in site_url else f"https://{site_url}")
    except Exception:
        return None
    host = (parsed.hostname or "").strip().lower()
    if not host or host in {"localhost", "_"}:
        return None
    if "*" in host:
        return None
    return host


def _normalize_domains(names: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        v = str(n or "").strip().lower()
        if not v or v in {"_", "localhost"}:
            continue
        if "*" in v:
            continue
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    out.sort(key=lambda x: (-_domain_preference_score(x), x))
    return out


def _pick_domain(names: list[str]) -> str | None:
    domains = _normalize_domains(names)
    return domains[0] if domains else None


def _parse_nginx_vhosts(path: Path) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    blocks = re.findall(r"server\s*\{([\s\S]*?)\n\}", text, flags=re.MULTILINE)
    for b in blocks:
        roots = [m.strip().strip("'\"") for m in re.findall(r"^\s*root\s+([^;]+);", b, flags=re.MULTILINE)]
        names_raw = [m.strip() for m in re.findall(r"^\s*server_name\s+([^;]+);", b, flags=re.MULTILINE)]
        names: list[str] = []
        for raw in names_raw:
            names.extend([x for x in raw.split() if x.strip()])
        domains = _normalize_domains(names)
        for r in roots:
            if r.startswith("/"):
                out.append((r, domains))
    return out


def _parse_apache_vhosts(path: Path) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    blocks = re.findall(r"<VirtualHost\b[^>]*>([\s\S]*?)</VirtualHost>", text, flags=re.IGNORECASE)
    for b in blocks:
        dr = re.search(r"^\s*DocumentRoot\s+(.+)$", b, flags=re.IGNORECASE | re.MULTILINE)
        if not dr:
            continue
        root = dr.group(1).strip().strip("'\"")
        if not root.startswith("/"):
            continue
        sn = re.search(r"^\s*ServerName\s+(.+)$", b, flags=re.IGNORECASE | re.MULTILINE)
        sa = re.findall(r"^\s*ServerAlias\s+(.+)$", b, flags=re.IGNORECASE | re.MULTILINE)
        names: list[str] = []
        if sn:
            names.extend([x for x in sn.group(1).split() if x.strip()])
        for raw in sa:
            names.extend([x for x in raw.split() if x.strip()])
        out.append((root, _normalize_domains(names)))
    return out


def _web_vhosts_from_server_configs() -> dict[str, list[str]]:
    out: list[tuple[str, list[str]]] = []
    nd = Path("/etc/nginx/sites-enabled")
    ad = Path("/etc/apache2/sites-enabled")
    if nd.exists():
        for p in nd.glob("*"):
            if p.is_file() or p.is_symlink():
                out.extend(_parse_nginx_vhosts(p))
    if ad.exists():
        for p in ad.glob("*"):
            if p.is_file() or p.is_symlink():
                out.extend(_parse_apache_vhosts(p))
    dedup: dict[str, set[str]] = {}
    for root, domains in out:
        cur = dedup.setdefault(root, set())
        for d in domains or []:
            dv = str(d or "").strip().lower()
            if dv:
                cur.add(dv)
    mapped: dict[str, list[str]] = {}
    for root in sorted(dedup.keys()):
        mapped[root] = _normalize_domains(list(dedup[root]))
    return mapped


def _detect_mautic_local_php(root: str) -> str | None:
    p4 = Path(root) / "app" / "config" / "local.php"
    if p4.exists():
        return str(p4)
    p5 = Path(root) / "config" / "local.php"
    if p5.exists():
        return str(p5)
    return None


def _is_retired_instance_root(root: str) -> bool:
    try:
        return (Path(root) / RETIRED_INSTANCE_MARKER).exists()
    except OSError:
        return False


def _resolve_discovery_paths(vhost_root: str) -> tuple[str, str | None, str | None, list[str]]:
    """
    Resolve effective Mautic install root for autodiscovery.

    For zip installs, vhost root is usually install root.
    For composer installs, vhost root can be docroot/public while
    console+config live one level up.
    """
    raw = Path(vhost_root)
    checked: list[Path] = []
    markers: list[str] = []

    def _push_candidate(p: Path) -> None:
        if p not in checked:
            checked.append(p)

    _push_candidate(raw)
    _push_candidate(raw.resolve())
    _push_candidate(raw.parent)
    _push_candidate(raw.parent.parent)
    if raw.resolve() != raw:
        _push_candidate(raw.resolve().parent)
        _push_candidate(raw.resolve().parent.parent)

    for candidate in checked:
        if not candidate.exists() or not candidate.is_dir():
            continue
        root = str(candidate)
        local_php = _detect_mautic_local_php(root)
        console_path = _find_console_path(root)
        if local_php and console_path:
            if candidate != raw:
                markers.append("resolved-from-vhost-root")
            if raw.name.lower() in {"docroot", "public", "public_html"} and candidate in {raw.parent, raw.resolve().parent}:
                markers.append("composer-docroot")
            return root, local_php, console_path, markers

    return str(raw), None, None, markers


def discover_mautic(
    roots: list[str],
    exclude_path_contains: list[str],
    supported_mautic_majors: list[int] | None = None,
    custom_instances: list[ManualInstanceConfig] | None = None,
) -> list[MauticInstall]:
    installs: list[MauticInstall] = []
    allowed = set(supported_mautic_majors or [4, 5, 6, 7])
    vhost_map = _web_vhosts_from_server_configs()
    configured_roots = list(vhost_map.keys())
    fallback_roots = [r for r in (roots or []) if r not in vhost_map]
    all_roots = configured_roots + fallback_roots

    for root in all_roots:
        if not Path(root).exists():
            continue
        if any(token in root for token in exclude_path_contains):
            continue
        resolved_root, local_php, console_path, path_markers = _resolve_discovery_paths(root)
        if not local_php:
            continue
        if not console_path:
            continue
        if _is_retired_instance_root(resolved_root):
            continue
        marker = "bin/console" if console_path.endswith("bin/console") else "app/console"
        major = _detect_mautic_major(resolved_root, local_php)
        if major is not None and major not in allowed:
            continue
        vhost_domains = list(vhost_map.get(root, []) or [])
        vhost_domain = vhost_domains[0] if vhost_domains else None
        config_domain = _domain_from_local_php(local_php)
        domain = config_domain or vhost_domain
        if config_domain and vhost_domain and config_domain != vhost_domain:
            # Template clones often keep stale site_url (default*.sales-snap.*)
            # until app-level post-clone tuning runs. Prefer real vhost domain.
            if _is_template_domain(config_domain) and not _is_template_domain(vhost_domain):
                domain = vhost_domain
        domains: list[str] = list(vhost_domains)
        if domain and domain not in domains:
            domains.insert(0, domain)
        domains = _normalize_domains(domains)
        if domain and domain in domains and domains[0] != domain:
            domains = [domain] + [x for x in domains if x != domain]
        name = domain or (Path(resolved_root).name or "mautic")
        installs.append(
            MauticInstall(
                instance_uid=build_domain_uid(domain=domain, root=resolved_root, name=name),
                name=name,
                root=resolved_root,
                primary_domain=domain,
                console_path=console_path,
                local_php_path=local_php,
                mautic_timezone=_read_mautic_timezone(local_php),
                mautic_major=major,
                db=_build_db(local_php),
                source="autodiscovery",
                markers=[marker, "app/config/local.php|config/local.php", "sites-enabled"] + path_markers,
                domains=domains,
            )
        )

    for item in custom_instances or []:
        console_path = _find_console_path(item.root, item.console_path)
        if not console_path:
            continue
        local_php_path = item.local_php_path
        if not local_php_path:
            p1 = Path(item.root) / "app" / "config" / "local.php"
            p2 = Path(item.root) / "config" / "local.php"
            local_php_path = str(p1 if p1.exists() else p2 if p2.exists() else "")
            if not local_php_path:
                local_php_path = None
        installs.append(
            MauticInstall(
                instance_uid=build_domain_uid(domain=None, root=item.root, name=item.name),
                name=item.name,
                root=item.root,
                primary_domain=None,
                console_path=console_path,
                local_php_path=local_php_path,
                mautic_timezone=_read_mautic_timezone(local_php_path),
                mautic_major=_detect_mautic_major(item.root, local_php_path, item.mautic_major),
                db=_build_db(local_php_path, item),
                source="manual",
                markers=["manual-instance"],
                domains=[],
            )
        )

    dedup: dict[str, MauticInstall] = {}
    for inst in installs:
        if inst.mautic_major is not None and inst.mautic_major not in allowed:
            continue
        dedup[inst.root] = inst

    return list(dedup.values())
