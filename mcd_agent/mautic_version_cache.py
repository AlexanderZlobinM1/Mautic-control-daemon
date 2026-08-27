from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from mcd_agent.config import AgentConfig
from mcd_agent.discovery import discover_mautic
from mcd_agent.models import MauticInstall

_SEMVER_RE = re.compile(r"(\d+\.\d+\.\d+)")
_VERSION_CACHE_ROOT = Path("/opt/mcd/generated/mautic-version")
_LEGACY_VERSION_CACHE_REL = Path(".mcd") / "mautic.version"
_ZABBIX_HELPER_PATH = Path("/usr/local/bin/zbx_mautic_version_cached.sh")
_ZABBIX_CONF_PATH = Path("/etc/zabbix/zabbix_agent2.d/mautic.conf")
def version_cache_path(root: str | Path) -> Path:
    normalized = os.path.abspath(str(root))
    key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return _VERSION_CACHE_ROOT / f"{key}.version"


def _legacy_version_cache_path(root: str | Path) -> Path:
    return Path(root) / _LEGACY_VERSION_CACHE_REL


def read_cached_mautic_version(root: str | Path) -> str | None:
    path = version_cache_path(root)
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    if raw and raw != "-":
        return raw.splitlines()[0].strip() or None
    return None


def migrate_legacy_mautic_version_cache(root: str | Path) -> bool:
    legacy = _legacy_version_cache_path(root)
    try:
        raw = legacy.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return False
    if not raw or raw == "-":
        return False
    return write_mautic_version_cache(root, raw.splitlines()[0])


def _atomic_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_mautic_version_cache(root: str | Path, version: str) -> bool:
    root_path = Path(root)
    if not root_path.exists() or not root_path.is_dir():
        return False
    safe = str(version or "").strip() or "-"
    if "\n" in safe:
        safe = safe.splitlines()[0].strip() or "-"
    try:
        _atomic_write_text(version_cache_path(root_path), safe + "\n")
        return True
    except OSError:
        return False


def _candidate_roots(root: str) -> list[Path]:
    root_path = Path(root)
    candidates: list[Path] = [root_path]
    base = root_path.name.lower()
    if base in {"public", "docroot", "public_html"}:
        candidates.append(root_path.parent)
    out: list[Path] = []
    seen: set[str] = set()
    for p in candidates:
        ps = str(p)
        if ps == "/" or ps in seen:
            continue
        seen.add(ps)
        out.append(p)
    return out


def _version_major(version: str | None) -> int | None:
    match = _SEMVER_RE.search(str(version or ""))
    if not match:
        return None
    try:
        return int(match.group(1).split(".", 1)[0])
    except ValueError:
        return None


def _read_version_from_composer_lock(root: Path) -> str | None:
    lock = root / "composer.lock"
    if not lock.exists():
        return None
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except Exception:
        return None
    packages = data.get("packages", [])
    if not isinstance(packages, list):
        return None
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        if str(pkg.get("name", "")).strip() not in {"mautic/core-lib", "mautic/core-bundle", "mautic/core"}:
            continue
        version = str(pkg.get("version", "")).strip()
        match = _SEMVER_RE.search(version)
        if match:
            return match.group(1)
    return None


def _console_path_for_root(root: Path, console_path: str | None = None) -> Path | None:
    if console_path:
        supplied = Path(console_path)
        if supplied.exists() and supplied.is_file():
            return supplied
    for rel in ("bin/console", "app/console"):
        candidate = root / rel
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _read_version_from_mcd_source(
    root: Path,
    php_bin: str,
    console_path: str | None = None,
    *,
    run_as_user: str | None = "www-data",
) -> str | None:
    console = _console_path_for_root(root, console_path)
    if not console:
        return None
    try:
        # Avoid a module cycle: mautic_upgrade also refreshes this shared cache.
        from mcd_agent.mautic_upgrade import _read_current_version

        value = _read_current_version(str(root), str(console), php_bin, run_as_user).strip()
    except Exception:
        return None
    if value and value != "0.0.0" and _SEMVER_RE.search(value):
        return value
    return None


def _cache_roots(original_root: str, detected_root: Path | None = None) -> list[Path]:
    roots: list[Path] = []
    for item in [Path(original_root), detected_root]:
        if item is None:
            continue
        if item.exists() and item.is_dir() and item not in roots:
            roots.append(item)
    return roots


def collect_mautic_version(
    root: str,
    php_bin: str,
    *,
    console_path: str | None = None,
    update_cache: bool = True,
    run_as_user: str | None = "www-data",
    force_refresh: bool = False,
    expected_major: int | None = None,
) -> str:
    detected_root: Path | None = None
    version: str | None = None

    # Regular state pushes must be lightweight and must not start Mautic console
    # every few seconds. A forced refresh is used by the explicit Zabbix/cache
    # guard and runs the console as the Mautic runtime user, not root.
    if not force_refresh:
        for candidate in _candidate_roots(root):
            version = read_cached_mautic_version(candidate)
            if version and expected_major is not None and _version_major(version) != int(expected_major):
                version = None
            if version:
                detected_root = candidate
                break

    if not version:
        for candidate in _candidate_roots(root):
            version = _read_version_from_mcd_source(candidate, php_bin, console_path, run_as_user=run_as_user)
            if version:
                detected_root = candidate
                break

    if not version:
        # Last resort only: composer.lock can lag behind patched runtime state on
        # some migrated installs, so do not prefer it over the Mautic console.
        for candidate in _candidate_roots(root):
            version = _read_version_from_composer_lock(candidate)
            if version:
                detected_root = candidate
                break

    value = version or "-"
    if update_cache and value != "-":
        for cache_root in _cache_roots(root, detected_root):
            write_mautic_version_cache(cache_root, value)
    return value


def refresh_mautic_version_cache(
    installs: list[MauticInstall],
    php_bin: str,
    *,
    run_as_user: str | None = "www-data",
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    ok = 0
    for inst in installs:
        version = collect_mautic_version(
            inst.root,
            php_bin,
            console_path=inst.console_path,
            update_cache=True,
            run_as_user=run_as_user,
            force_refresh=True,
            expected_major=inst.mautic_major,
        )
        cache = version_cache_path(inst.root)
        cached = cache.exists() and cache.is_file()
        if cached and version != "-":
            ok += 1
        rows.append(
            {
                "instance_uid": inst.instance_uid,
                "name": inst.name,
                "root": inst.root,
                "version": version,
                "cache_path": str(cache),
                "cached": cached,
            }
        )
    return {"status": "ok", "updated": ok, "instances": rows}


def discover_and_refresh_mautic_version_cache(cfg: AgentConfig) -> dict[str, Any]:
    installs = discover_mautic(
        cfg.discovery_roots,
        cfg.exclude_path_contains,
        supported_mautic_majors=cfg.supported_mautic_majors,
        custom_instances=cfg.custom_instances,
    )
    return refresh_mautic_version_cache(installs, cfg.php_bin, run_as_user=cfg.mautic_run_as_user)


def retire_zabbix_mautic_version_userparameter() -> dict[str, Any]:
    """Remove the retired per-instance Zabbix version probe without touching other checks."""
    changed = False
    if _ZABBIX_CONF_PATH.exists():
        try:
            lines = _ZABBIX_CONF_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
            kept = [line for line in lines if not line.strip().startswith("UserParameter=mautic.version[")]
            if kept != lines:
                _atomic_write_text(_ZABBIX_CONF_PATH, "\n".join(kept).rstrip() + ("\n" if kept else ""))
                changed = True
        except OSError as exc:
            return {"status": "error", "reason": str(exc), "changed": False}
    try:
        if _ZABBIX_HELPER_PATH.exists():
            _ZABBIX_HELPER_PATH.unlink()
            changed = True
        legacy_backup = _ZABBIX_CONF_PATH.with_suffix(_ZABBIX_CONF_PATH.suffix + ".mcd-bak")
        if legacy_backup.exists():
            legacy_backup.unlink()
            changed = True
    except OSError as exc:
        return {"status": "error", "reason": str(exc), "changed": changed}
    if not changed:
        return {"status": "ok", "changed": False, "restarted": False}
    try:
        active = subprocess.run(
            ["systemctl", "is-active", "zabbix-agent2"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if active.returncode == 0 and (active.stdout or "").strip() == "active":
            restarted = subprocess.run(
                ["systemctl", "restart", "zabbix-agent2"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if restarted.returncode != 0:
                return {
                    "status": "error",
                    "reason": (restarted.stderr or restarted.stdout or "zabbix restart failed").strip(),
                    "changed": True,
                    "restarted": False,
                }
            return {"status": "ok", "changed": True, "restarted": True}
    except OSError as exc:
        return {"status": "error", "reason": str(exc), "changed": True, "restarted": False}
    return {"status": "ok", "changed": True, "restarted": False}
