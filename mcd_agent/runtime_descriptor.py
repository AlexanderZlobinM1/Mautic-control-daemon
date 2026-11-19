from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import urlparse

from mcd_agent.instance_uid import build_domain_uid
from mcd_agent.instance_identity import validate_instance_code
from mcd_agent.localphp import parse_local_php
from mcd_agent.models import DBConfig, MauticInstall


DESCRIPTOR_ROOT = Path("/etc/mcd/instances.d")
RUNTIME_MARKER = ".mcd-runtime.json"
_CONTAINER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_USER_RE = re.compile(r"^[0-9]{1,10}(?::[0-9]{1,10})?$")
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_DB_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:%_-]{0,254}$")
_INSTALL_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_ADAPTER_RE = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    path: Path
    host_root: Path
    runtime_root: str
    console_path: str
    local_php_path: Path
    container_name: str
    container_user: str
    php_bin: str
    host_db_host: str | None
    image_ref: str | None
    instance_uid: str
    name: str
    primary_domain: str | None
    domains: list[str]
    mautic_major: int | None
    install_type: str | None
    capabilities: frozenset[str]
    host_plugins_path: Path | None
    runtime_plugins_path: str | None
    host_composer_lock_path: Path | None
    migration_adapter: str | None
    instance_code: str | None = None

    def docker_exec_prefix(self) -> list[str]:
        return [
            "/usr/bin/docker",
            "exec",
            "--user",
            self.container_user,
            "--workdir",
            self.runtime_root,
            self.container_name,
            self.php_bin,
            self.console_path,
        ]

    def has_capability(self, name: str) -> bool:
        return str(name or "").strip().lower() in self.capabilities


def _absolute_path(value: object, *, field: str) -> Path:
    path = Path(str(value or "").strip())
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"runtime descriptor {field} must be an absolute path")
    return path


def _absolute_runtime_path(value: object, *, field: str) -> str:
    path = _absolute_path(value, field=field)
    return path.as_posix()


def _safe_regular_descriptor(path: Path, *, require_root_owner: bool) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"runtime descriptor must be a regular file: {path}")
    stat = path.stat()
    if require_root_owner and stat.st_uid != 0:
        raise ValueError(f"runtime descriptor must be owned by root: {path}")
    if stat.st_mode & 0o022:
        raise ValueError(f"runtime descriptor must not be group/world writable: {path}")


def load_runtime_descriptor(path: Path, *, require_root_owner: bool = True) -> RuntimeDescriptor:
    path = Path(path)
    _safe_regular_descriptor(path, require_root_owner=require_root_owner)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid runtime descriptor JSON: {path}") from exc
    if not isinstance(raw, dict) or int(raw.get("schema") or 0) != 1:
        raise ValueError(f"unsupported runtime descriptor schema: {path}")
    if str(raw.get("runtime") or "").strip().lower() != "docker":
        raise ValueError(f"unsupported runtime type in descriptor: {path}")

    host_root = _absolute_path(raw.get("host_root"), field="host_root")
    local_php = _absolute_path(raw.get("local_php_path"), field="local_php_path")
    try:
        local_php.relative_to(host_root)
    except ValueError as exc:
        raise ValueError("runtime descriptor local_php_path must be below host_root") from exc
    if not host_root.is_dir() or not local_php.is_file():
        raise ValueError(f"runtime descriptor host data is missing: {path}")

    runtime_root = _absolute_runtime_path(raw.get("runtime_root"), field="runtime_root")
    console_path = _absolute_runtime_path(raw.get("console_path"), field="console_path")
    if not console_path.startswith(runtime_root.rstrip("/") + "/"):
        raise ValueError("runtime descriptor console_path must be below runtime_root")
    php_bin = _absolute_runtime_path(raw.get("php_bin") or "/usr/bin/php", field="php_bin")
    container = str(raw.get("container_name") or "").strip()
    if not _CONTAINER_RE.fullmatch(container):
        raise ValueError("runtime descriptor has invalid container_name")
    runtime_user = str(raw.get("container_user") or "").strip()
    if not _USER_RE.fullmatch(runtime_user):
        raise ValueError("runtime descriptor container_user must be a numeric uid[:gid]")
    image_ref = str(raw.get("image_ref") or "").strip() or None
    install_type = str(raw.get("install_type") or "").strip().lower() or None
    if install_type and not _INSTALL_TYPE_RE.fullmatch(install_type):
        raise ValueError("runtime descriptor has invalid install_type")
    capabilities = frozenset(
        value
        for value in (str(item or "").strip().lower() for item in raw.get("capabilities") or [])
        if value and _CAPABILITY_RE.fullmatch(value)
    )
    migration_adapter = str(raw.get("migration_adapter") or "").strip().lower() or None
    if migration_adapter and not _ADAPTER_RE.fullmatch(migration_adapter):
        raise ValueError("runtime descriptor has invalid migration_adapter")

    host_plugins_path: Path | None = None
    runtime_plugins_path: str | None = None
    host_composer_lock_path: Path | None = None
    if raw.get("host_plugins_path") is not None or raw.get("runtime_plugins_path") is not None:
        if raw.get("host_plugins_path") is None or raw.get("runtime_plugins_path") is None:
            raise ValueError("runtime descriptor plugin paths must be declared together")
        host_plugins_path = _absolute_path(raw.get("host_plugins_path"), field="host_plugins_path")
        try:
            host_plugins_path.relative_to(host_root)
        except ValueError as exc:
            raise ValueError("runtime descriptor host_plugins_path must be below host_root") from exc
        runtime_plugins_path = _absolute_runtime_path(
            raw.get("runtime_plugins_path"), field="runtime_plugins_path"
        )
        if not runtime_plugins_path.startswith(runtime_root.rstrip("/") + "/"):
            raise ValueError("runtime descriptor runtime_plugins_path must be below runtime_root")
        if "plugin-write" in capabilities and not host_plugins_path.is_dir():
            raise ValueError("runtime descriptor writable plugin path is missing")

    if raw.get("host_composer_lock_path") is not None:
        host_composer_lock_path = _absolute_path(
            raw.get("host_composer_lock_path"), field="host_composer_lock_path"
        )
        try:
            host_composer_lock_path.relative_to(host_root)
        except ValueError as exc:
            raise ValueError("runtime descriptor host_composer_lock_path must be below host_root") from exc
        if not host_composer_lock_path.is_file():
            raise ValueError("runtime descriptor composer lock metadata is missing")
    host_db_host = str(raw.get("host_db_host") or "").strip() or None
    if host_db_host and not _DB_HOST_RE.fullmatch(host_db_host):
        raise ValueError("runtime descriptor has invalid host_db_host")

    domains: list[str] = []
    for item in raw.get("domains") or []:
        value = str(item or "").strip().lower().rstrip(".")
        if value and _DOMAIN_RE.fullmatch(value) and value not in domains:
            domains.append(value)
    primary = str(raw.get("primary_domain") or "").strip().lower().rstrip(".") or None
    if primary and not _DOMAIN_RE.fullmatch(primary):
        raise ValueError("runtime descriptor has invalid primary_domain")
    if primary and primary not in domains:
        domains.insert(0, primary)
    name = str(raw.get("name") or primary or host_root.name).strip()[:255]
    instance_uid = str(raw.get("instance_uid") or "").strip()
    if not instance_uid:
        instance_uid = build_domain_uid(domain=primary, root=str(host_root), name=name)
    instance_code_raw = str(raw.get("instance_code") or "").strip()
    instance_code = validate_instance_code(instance_code_raw) if instance_code_raw else None
    major_raw = raw.get("mautic_major")
    try:
        major = int(major_raw) if major_raw is not None else None
    except (TypeError, ValueError):
        major = None
    return RuntimeDescriptor(
        path=path,
        host_root=host_root,
        runtime_root=runtime_root,
        console_path=console_path,
        local_php_path=local_php,
        container_name=container,
        container_user=runtime_user,
        php_bin=php_bin,
        host_db_host=host_db_host,
        image_ref=image_ref,
        instance_uid=instance_uid,
        instance_code=instance_code,
        name=name,
        primary_domain=primary,
        domains=domains,
        mautic_major=major,
        install_type=install_type,
        capabilities=capabilities,
        host_plugins_path=host_plugins_path,
        runtime_plugins_path=runtime_plugins_path,
        host_composer_lock_path=host_composer_lock_path,
        migration_adapter=migration_adapter,
    )


def descriptor_for_root(root: str, *, require_root_owner: bool = True) -> RuntimeDescriptor | None:
    marker = Path(root) / RUNTIME_MARKER
    if not marker.is_file() or marker.is_symlink():
        return None
    descriptor = load_runtime_descriptor(marker, require_root_owner=require_root_owner)
    if descriptor.host_root.resolve() != Path(root).resolve():
        raise ValueError("runtime descriptor host_root does not match selected root")
    return descriptor


def _db_config(local_php: Path, *, host_override: str | None = None) -> DBConfig | None:
    values = parse_local_php(str(local_php))
    host = str(values.get("db_host") or "").strip()
    name = str(values.get("db_name") or "").strip()
    user = str(values.get("db_user") or "").strip()
    password = str(values.get("db_password") or "")
    if not (host and name and user and password):
        return None
    try:
        port = int(values.get("db_port") or 3306)
    except (TypeError, ValueError):
        port = 3306
    return DBConfig(
        host=host_override or host,
        port=port,
        name=name,
        user=user,
        password=password,
        table_prefix=str(values.get("db_table_prefix") or ""),
    )


def discover_runtime_instances(
    descriptor_root: Path = DESCRIPTOR_ROOT,
    *,
    supported_mautic_majors: list[int] | None = None,
    require_root_owner: bool = True,
) -> list[MauticInstall]:
    root = Path(descriptor_root)
    if not root.is_dir():
        return []
    allowed = set(supported_mautic_majors or [4, 5, 6, 7])
    installs: list[MauticInstall] = []
    for path in sorted(root.glob("*.json")):
        try:
            descriptor = load_runtime_descriptor(path, require_root_owner=require_root_owner)
        except (OSError, ValueError):
            continue
        if descriptor.mautic_major is not None and descriptor.mautic_major not in allowed:
            continue
        values = parse_local_php(str(descriptor.local_php_path))
        timezone = str(values.get("default_timezone") or values.get("timezone") or "").strip() or None
        primary = descriptor.primary_domain
        if not primary:
            site_url = str(values.get("site_url") or "").strip()
            parsed = urlparse(site_url if "://" in site_url else f"https://{site_url}")
            candidate = str(parsed.hostname or "").strip().lower()
            primary = candidate if candidate and _DOMAIN_RE.fullmatch(candidate) else None
        domains = list(descriptor.domains)
        if primary and primary not in domains:
            domains.insert(0, primary)
        installs.append(
            MauticInstall(
                instance_uid=descriptor.instance_uid,
                instance_code=descriptor.instance_code,
                name=descriptor.name,
                root=str(descriptor.host_root),
                console_path=descriptor.console_path,
                primary_domain=primary,
                local_php_path=str(descriptor.local_php_path),
                mautic_timezone=timezone,
                mautic_major=descriptor.mautic_major,
                db=_db_config(
                    descriptor.local_php_path,
                    host_override=descriptor.host_db_host,
                ),
                source="runtime-descriptor",
                markers=["docker-runtime", str(path)],
                domains=domains,
                runtime="docker",
                runtime_id=descriptor.container_name,
                runtime_root=descriptor.runtime_root,
                runtime_user=descriptor.container_user,
                runtime_php_bin=descriptor.php_bin,
                runtime_image_ref=descriptor.image_ref,
                install_type=descriptor.install_type,
                runtime_capabilities=sorted(descriptor.capabilities),
                runtime_adapter=descriptor.migration_adapter,
            )
        )
    return installs
